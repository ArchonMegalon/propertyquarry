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

## Local verification

```bash
npm ci
npm run sync
npm run test:web
npm run android:preview:container
```

The container build pins the Android image by digest, persists Gradle downloads and a preview-only debug signing identity in separate Docker volumes, and runs unit tests, lint, APK packaging and instrumentation-APK packaging from a clean tree. The preview APK is written to `android/app/build/outputs/apk/preview/app-preview.apk`.

## Release signing

Release builds fail closed unless all four values are present and the keystore path exists:

- `PROPERTYQUARRY_ANDROID_KEYSTORE_PATH`
- `PROPERTYQUARRY_ANDROID_KEYSTORE_PASSWORD`
- `PROPERTYQUARRY_ANDROID_KEY_ALIAS`
- `PROPERTYQUARRY_ANDROID_KEY_PASSWORD`

No signing material belongs in Git. Google Play App Signing should own the production app-signing key; the local key is an upload key only. Publish the Play app-signing SHA-256 fingerprint through `PROPERTYQUARRY_ANDROID_APP_LINK_SHA256_CERTS`, then verify the live Asset Links response before rollout.

## Push posture

The manifest declares Android 13 notification capability, while Firebase configuration remains optional and secret-free. No `google-services.json` is committed. A release must not claim working push delivery until a PropertyQuarry-owned Firebase project, consent UI, token registration endpoint, deletion flow, and end-to-end receipt are configured.
