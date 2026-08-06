# PropertyQuarry next-session handoff

Updated: 2026-08-06 12:10 UTC

## Mission

Finish the Android release by waiting for Google Play to approve the pending
PropertyQuarry upload-key reset, then upload the already signed `1.1.0`
(`versionCode 2`) bundle to **internal testing only**. Do not create, edit, or
roll out a production release.

## Current state

- Repository: `/docker/property`
- Branch: `integration/property-origin-main-20260728`
- Published implementation baseline before this handoff: `6785350b`
- Tracked working tree was clean immediately before this handoff was added.
- Google Play developer account: `9007890349240845326`
- Google Play app ID: `4976153363318887490`
- Package: `com.myexternalbrain.propertyquarry`
- Play Console currently reports: `There is a pending request for resetting
  the upload key of this app.`
- Reset reason: `I lost my upload key`
- Request submitted: 2026-08-06 around 12:10 UTC
- Production rollout changed: **no**

The old upload keystore is unavailable. EA has the Memorial upload key, not the
PropertyQuarry key; do not reuse the Memorial key across apps.

## Upload-key reset

Current Play upload certificate SHA-256:

```text
17:27:72:EE:6F:2A:8F:7F:55:D7:4B:76:53:90:5B:46:D2:63:89:F8:C1:7C:00:0D:D0:E0:87:7A:35:44:E2:5E
```

Requested PropertyQuarry upload certificate SHA-256:

```text
A8:88:7D:66:41:BF:71:35:E3:74:B0:D8:50:4C:84:1A:F5:D7:26:09:0F:B8:1E:AE:59:67:07:5B:60:81:16:CF
```

The public PEM was uploaded successfully and the request was submitted. Do not
cancel or submit a second reset. Wait until Play shows the requested fingerprint
as the active upload certificate.

Local signing material is outside Git and permission-restricted:

```text
/home/tibor/.local/share/propertyquarry/android-signing-v2/propertyquarry-upload.p12
/home/tibor/.local/share/propertyquarry/android-signing-v2/propertyquarry-upload-cert.pem
/home/tibor/.local/share/propertyquarry/android-signing-v2/android-release.env
```

Never print or commit values from `android-release.env`. Source it only for a
release build.

## Ready bundle

```text
Path: mobile/android/app/build/outputs/bundle/release/app-release.aab
Version name: 1.1.0
Version code: 2
SHA-256: a17256a66a73ae4de535361b0e9f324091584d96a0a6df692a296345d27dd7e6
```

The release build completed successfully with 232 Gradle tasks. Bundletool
validation passed, JAR signature verification passed, and the signer matched the
requested upload certificate exactly.

If regeneration is necessary:

```bash
source /home/tibor/.local/share/propertyquarry/android-signing-v2/android-release.env
cd /docker/property/mobile
npm run android:release:container
```

Re-check the resulting digest and signer before upload.

## BrowserAct continuation

The authenticated Google Play session was created in this conversation and may
still be available:

```text
Session: pq-play-stealth-auth
Browser ID: 111111582245966456
Browser name: google-play-propertyquarry-release-stealth
Browser type: stealth
```

Direct key-management URL:

```text
https://play.google.com/console/u/0/developers/9007890349240845326/app/4976153363318887490/keymanagement
```

Use the `browser-act` skill before issuing BrowserAct commands. Do not operate
sessions created by other conversations. Start by listing sessions; if
`pq-play-stealth-auth` exists, inspect its state. If authentication has expired,
use BrowserAct remote assist for the user to sign in and stop at the dashboard.
Do not enter or request the user's Google password.

The earlier remote viewer worked when its link was opened in Chrome/Safari
instead of Telegram's embedded browser. Old remote-assist URLs expire and must
not be reused.

BrowserAct local storage had broken symlinks to a missing `/dev/shm` capacity
directory. They were moved to:

```text
/home/tibor/.local/share/browseract/.capacity-link-backup-20260806/
```

Real mode-`0700` directories now exist for `kernels`, `profiles`, `browsers`,
and `ffmpeg`. Do not restore the broken links unless their targets exist again.

A trusted local Chromium profile was imported into browser
`chrome_local_111103671093166189`, but it contained no active Google session.
The successful Play login is in the stealth browser above.

## Exact resume sequence

1. Follow `AGENTS.md`: call vexp `run_pipeline` first for repository work.
2. Read and follow the `browser-act` and `ea-browser-ooda-operator` skills.
3. Check `pq-play-stealth-auth`, or obtain a fresh user login through remote
   assist if the session expired.
4. Open the key-management URL and verify the pending request. Do not click
   `Cancel request`.
5. Reload/check later until the active upload certificate SHA-256 is
   `A8:88:7D:66:...:16:CF` and the pending banner is gone.
6. Navigate to **Test and release -> Testing -> Internal testing**.
7. Create the next internal-testing release and upload
   `mobile/android/app/build/outputs/bundle/release/app-release.aab`.
8. Confirm Play reads `versionCode 2` / `1.1.0` and accepts the new upload
   signer. Resolve only internal-track validation errors.
9. Save/roll out to internal testers only. Do not touch production.
10. Capture the accepted release state and update
    `mobile/build/propertyquarry-google-play-evidence.json` for version 2.
11. Run `npm run android:release:readiness` and require zero failures/blockers.
12. Run vexp `verify_done`, check Git status, and commit/push any tracked
    handoff or evidence-contract changes.

The user's authorization covers completing the PropertyQuarry internal test
release. A production rollout is explicitly outside scope.

## Evidence and receipts

These build outputs are ignored by Git and may be regenerated:

```text
mobile/build/propertyquarry-upload-key-reset-receipt.json
mobile/build/propertyquarry-upload-key-reset-pending.png
mobile/build/propertyquarry-android-release-evidence.json
mobile/build/propertyquarry-android-test-phase-receipt.json
mobile/build/propertyquarry-play-remote-assist-telegram-receipt.json
```

Pending-reset screenshot SHA-256:

```text
bae826822a2470da6581ff5ef597c25753288d4f25ff1ad03f31659d7cc906f1
```

Public PEM file SHA-256:

```text
ace1e076f8efdca22439b556edec3de764829eb74637fafe22c69bd060ec7aa6
```

Telegram status message `5117` reported the pending reset, ready AAB, untouched
production, and pushed repository head.

## Verification already completed

- `npm run test:web`: 9/9 passed.
- `npm run sync`: passed.
- `npm run android:preview:container`: passed, 242 Gradle tasks.
- `pytest -q tests/test_propertyquarry_mobile_app_contract.py`: 6 passed.
- Launch surface rendered at 412 x 915 and was visually clean.
- A second full preview build proved that the release-bundle snapshot/restore
  guard preserves a sentinel byte-for-byte across Gradle `clean`.
- Signed release build: 232 Gradle tasks, bundletool valid, signer verified.

## Published commits

```text
136cc3c5 test(property): align handoff regression contracts
c3311cc8 fix(mobile): preserve release bundle during preview tests
6785350b chore(mobile): advance Android release to 1.1.0
```

All three are pushed to
`origin/integration/property-origin-main-20260728`.

## Important implementation note

`mobile/scripts/build-preview-container.sh` now snapshots and restores
`android/app/build/outputs/bundle/release` around the global Gradle `clean`.
Do not remove that guard: the original preview flow deleted signed release AABs.
The behavior is covered by `mobile/test/app-contract.test.mjs` and documented in
`mobile/README.md`.
