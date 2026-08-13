# PropertyQuarry Android

This directory owns the isolated Android release for PropertyQuarry. It is a thin Capacitor 8 client for the server-authoritative workspace at `https://propertyquarry.com`; it does not import Memorial code, sessions, databases, providers, credentials, or release state.

## Product contract

- Android package: `com.myexternalbrain.propertyquarry`
- Preview package: `com.myexternalbrain.propertyquarry.preview`
- Start surface: `/app/search`
- Normal camera walkthrough: primary/default
- 3DVista or Matterport: optional immersive 3D tour
- VR / 3D-glasses mode: optional inside a compatible tour, never the default
- Shared listing links: native confirmation, authenticated JSON POST, server validation and deduplication
- Google sign-in: external browser, short-lived one-time handoff, Android-generated S256 PKCE, HttpOnly WebView session
- App Links: fail closed until the release signing certificate is published by `/.well-known/assetlinks.json`

The production package ID is a frozen Google Play store identity for the existing listing. Its legacy namespace grants no MyExternalBrain operational authority, ownership, provider access, session access, or place in the PropertyQuarry trust boundary.

## Local verification

```bash
npm ci
npm run sync
npm run test:web
npm run android:preview:container
```

The container build pins the Android image by digest, persists Gradle downloads and a preview-only debug signing identity in separate Docker volumes, and runs unit tests, lint, APK packaging and instrumentation-APK packaging from a clean tree. An existing signed release-bundle directory is snapshotted and restored around that clean build so preview testing cannot invalidate release evidence. The preview APK is written to `android/app/build/outputs/apk/preview/app-preview.apk`.

## Release signing

Release builds fail closed unless all four values are present and the keystore path exists:

- `PROPERTYQUARRY_ANDROID_KEYSTORE_PATH`
- `PROPERTYQUARRY_ANDROID_KEYSTORE_PASSWORD`
- `PROPERTYQUARRY_ANDROID_KEY_ALIAS`
- `PROPERTYQUARRY_ANDROID_KEY_PASSWORD`

No signing material belongs in Git. Google Play App Signing should own the production app-signing key; the local key is an upload key only. Publish the Play app-signing SHA-256 fingerprint through `PROPERTYQUARRY_ANDROID_APP_LINK_SHA256_CERTS`, then verify the live Asset Links response before rollout.

Provision a dedicated upload identity into an explicit directory outside the repository. The command fails closed instead of replacing an existing identity:

```bash
PROPERTYQUARRY_ANDROID_SIGNING_DIR=/absolute/private/path \
  npm run android:upload-key:provision
```

The command creates a mode-`0600` PKCS#12 keystore, a public PEM certificate, and a mode-`0600` `android-release.env` file. Source that file only in the release shell, then use the digest-pinned Android build image:

```bash
set -a
. /absolute/private/path/android-release.env
set +a
npm run android:release:container
```

The release container runs the web/store contract tests, release unit tests and lint, builds the signed AAB, validates it with checksum-pinned Google Bundletool `1.18.3`, enforces the production package/version/SDK contract, verifies its JAR signature and exact upload-certificate identity, prints the public signing certificate, and writes `android/app/build/outputs/bundle/release/app-release.aab`. It also creates the ignored, secret-free receipt `build/propertyquarry-android-release-evidence.json`. Passwords are inherited by name and are never included in the Docker command line or build log.

After Play Console setup, save a redacted `propertyquarry.android.play_evidence.v1` receipt as `build/propertyquarry-google-play-evidence.json`, then run:

```bash
npm run android:release:readiness
```

The verifier cross-checks both receipts, the exact AAB digest and current Git commit against the live runtime contract, privacy page and Digital Asset Links. Exit `0` means release-ready, exit `2` means only external Play/App-Link proof is pending, and exit `1` means local evidence is invalid. An internal or closed test upload is sufficient for readiness; a production rollout remains a separate irreversible approval.

## Push posture

The manifest declares Android 13 notification capability, while Firebase configuration remains optional and secret-free. No `google-services.json` is committed. A release must not claim working push delivery until a PropertyQuarry-owned Firebase project, consent UI, token registration endpoint, deletion flow, and end-to-end receipt are configured.
