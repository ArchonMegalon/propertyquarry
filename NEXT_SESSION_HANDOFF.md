# PropertyQuarry next-session handoff

Updated: 2026-08-09 12:05 UTC

## Mission

Complete the prepared PropertyQuarry `1.1.0` (`versionCode 2`) release on the
Google Play **internal testing** track after the upload-key security cooldown
ends. Do not create, edit, promote, or roll out a production release.

The repository audit and repair pass is represented by the published commit
that contains this handoff. Start from that commit and preserve its clean signed
release evidence.

## Secure sign-in incident and repair

The installed internal build was observed hanging on `Finishing secure sign-in`
after returning from Google. Live API access logs proved that the app loaded
`/mobile/runtime-contract`, `/mobile/auth/bridge`, and `/mobile/bridge.js`, but
never sent `POST /mobile/auth/redeem`. The server-side OAuth callback, PKCE
exchange, HttpOnly WebView cookie, and redirect path remain covered by the green
identity route suite. The failure boundary was the Android native bridge call
before redemption.

This handoff commit repairs both sides of that boundary:

- `MainActivity` now defers pending auth/share navigation until the activity is
  resumed and the runtime contract is ready, and prevents duplicate initial
  navigation.
- The mobile bridge now waits briefly for Android resume, bounds native calls
  and redemption fetches with timeouts, exposes honest progress states, and
  offers an actionable retry instead of spinning forever.

The server-side bridge repair was initially hot-patched into the running
`propertyquarry-api` container at 2026-08-06 16:52 UTC. The container returned
healthy, local readiness and bridge probes returned HTTP 200, and the public
Cloudflare response contained the new wake-up retry text with `Cache-Control:
no-store`. That writable-container phase is historical and was superseded by
the immutable deployment recorded below.

The Android lifecycle half cannot reach testers until version 2 is accepted on
the internal track after the upload-key cooldown. Until then, the deployed web
bridge gives the installed version 1.0 app a bounded retry path rather than an
endless spinner.

A follow-up audit closed three remaining bridge gaps in the handoff commit:

- successful auth/share cleanup now uses synchronous durable preference commits
  and is awaited before navigation, preventing consumed handoffs from surviving
  as stale local state after a process interruption;
- every native and network operation in both auth and share modes now has a
  bounded timeout and actionable retry state; and
- `/mobile/bridge.js` explicitly declares UTF-8, preventing non-ASCII progress
  copy from being decoded differently by standalone or embedded clients.

The running API hotpatch was refreshed with these web changes at 2026-08-06
20:20 UTC. Container health, local readiness, the public UTF-8/no-store headers,
and BrowserAct-rendered bridge content all passed. It remained a writable-layer
hotfix until the immutable deployment recorded below.

The 2026-08-07 polish pass upgraded the mobile handoff without changing its
security boundary:

- the auth and share bridges now use a compact three-stage progress card with
  honest working, completed, and failed visual states;
- retry actions appear only inside the PropertyQuarry app, so an external
  browser receives a clear return-to-app instruction instead of a useless
  button;
- bridge UI and runtime messages are coherent in English, German, and Spanish
  using the device `Accept-Language` preference;
- status updates are polite live regions, progress exposes `aria-busy`, focus
  treatment is explicit, and reduced-motion/forced-colors modes are covered;
  and
- the privacy note explains the narrow Google identity boundary and the share
  bridge confirms that only the user-approved listing link is imported.

The polished web bridge was visually checked in BrowserAct in German, including
the external-browser failure state, and hot-patched into the healthy live API.
That intermediate hotpatch was superseded by the immutable deployment below.

The 2026-08-07 minimal refinement then removed the framed checklist and purely
decorative card ornament, narrowed the surface, reduced type and shadow weight,
and replaced the visible step labels with a compact three-point progress rail.
The labels remain in the semantic ordered list for assistive technology. The
localized failure state was rendered again in BrowserAct, and the cache-busted
stylesheet was hot-patched into the healthy live API. No sign-in behavior,
native contract, or Android binary content changed in this refinement.

The 2026-08-07 public-product polish aligned the landing and sample shortlist
with the same minimal premium language: editorial serif headings, quieter
translucent navigation, restrained depth, pill actions, circular result tabs,
and a narrow brass rail for the selected home. The skip-to-content control now
appears only for visible keyboard focus, fixing the automated-screenshot
artifact without weakening keyboard access. The landing still fits its
single-desktop-viewport contract. Landing and selected-result states were
visually checked in BrowserAct and the three public templates were hot-patched
into the healthy live API. That intermediate hotpatch was superseded by the
immutable deployment below.

The 2026-08-09 demo-conversation pass is published in source commit
`55bd54d7724bace15bd91be9f13d74806fc5a9c6` and is live on the public example
shortlist. It adds a visible, reusable question-and-answer transcript for the
selected demo home, a public read-only decision-answer endpoint, prompt chips,
and responsive desktop/mobile presentation. Answers are grounded in canonical
facts for the selected sample, German viewing questions are recognized, and
unknown facts no longer become fabricated negative signals such as `no_lift`.
The full PropertyQuarry browser gate passed with `129 passed, 1 skipped`; the
focused conversation/decision browser gate passed `3`, and the affected API,
feedback, and shortlist contracts passed `9`.

The three changed runtime files were hot-patched into `propertyquarry-api` at
2026-08-09 11:43 UTC. The first restart exposed copied Python modes of `0600`
and stopped at import with `PermissionError`; the files were immediately
re-copied as `0644`, after which the container returned `running healthy` with
restart count `0`. Public readiness returned HTTP 200, the live page exposed
the conversation controls without horizontal overflow, and a synthetic public
question returned the selected title plus the real open parking issue. A
recoverable pre-patch copy remains under
`/tmp/propertyquarry-conversation-live-backup.5OfVx1` until host cleanup or
reboot.

## Immutable web deployment completed

All writable-container caveats above are now closed. At 2026-08-09 12:01 UTC,
`propertyquarry-api` was recreated from the clean, published repository HEAD
`e0ea2376173151be4432930d7da01a0c7463a19e` using the pinned
`ea/Dockerfile.property-web` runtime contract.

```text
Image tag: propertyquarry-standalone-web-runtime:local-e0ea23761731
Image ID: sha256:92974baf94cc172fc9c1b038b634fed50850451ab2ca37cbf78e4a249f25cf9e
OCI revision: e0ea2376173151be4432930d7da01a0c7463a19e
Deployment ID: propertyquarry-conversation-e0ea2376-20260809
Container: propertyquarry-api
State after replacement: running healthy, restart count 0
Bound port: 127.0.0.1:8097 -> 8090/tcp
```

Only the API service was force-recreated with `--no-deps --no-build`. Its
configuration binds, reviewer trust store, named data volumes, network
membership, `on-failure:3` restart policy, and public port were preserved.
Database, migration data, worker, scheduler, render tools, backup, and
Cloudflare tunnel were not recreated.

The pre-replacement, working conversation hotpatch is retained as a local
rollback image:

```text
Tag: propertyquarry-standalone-web-runtime:conversation-hotpatch-rollback-20260809
Image ID: sha256:896e1ff54155fa2ee9097830d213bf9615e175cdc08ebd42f6f38a8a6cc03e06
```

Post-deployment checks verified that the runtime files are baked into the new
image at mode `0644`, the runtime release SHA and image ID match the values
above, `/health/ready` reports the PostgreSQL schema-v20 ready reason, and the
public example shortlist contains the selected conversation controls. English
and German synthetic questions returned grounded answers; the English answer
named the real parking gap and did not invent a missing lift. BrowserAct's
JavaScript-rendered extraction also exposed the complete conversation surface
and selected candidate. The prior full BrowserAct layout check against the same
published UI bytes found no horizontal overflow.

## Release state

- Repository: `/docker/property`
- Branch: `integration/property-origin-main-20260728`
- Google Play developer account: `9007890349240845326`
- Google Play app ID: `4976153363318887490`
- Package: `com.myexternalbrain.propertyquarry`
- Target release: `1.1.0` / `versionCode 2`
- Track: internal testing only
- Internal release draft: `2`
- Active internal release remains `1.0` until draft 2 is accepted and rolled
  out.
- Production rollout changed: **no**

The replacement PropertyQuarry upload certificate is now active in Play:

```text
A8:88:7D:66:41:BF:71:35:E3:74:B0:D8:50:4C:84:1A:F5:D7:26:09:0F:B8:1E:AE:59:67:07:5B:60:81:16:CF
```

Play enforces a post-reset security cooldown. The prepared bundle cannot be
uploaded before:

```text
2026-08-08 12:09:53 UTC
2026-08-08 14:09:53 Europe/Vienna
```

The exact Play message was:

```text
You uploaded an app bundle that is signed with an upload certificate that is not yet valid because it has been recently reset. You will be able to upload app bundles again from 8 Aug 2026, 12:09:53 UTC.
```

Do not cancel the reset, request another reset, or repeatedly retry before the
eligible time.

## Signed bundle

```text
Path: mobile/android/app/build/outputs/bundle/release/app-release.aab
Version name: 1.1.0
Version code: 2
SHA-256: d01c419ccd2b3efd186b89f1709cb44cca213c72aab2dc849c1eddbdf79b0ff7
Min SDK: 24
Target SDK: 36
```

The release build completed with 232 Gradle tasks. Web contracts, Android unit
tests, Android lint, bundletool validation, JAR signature validation, and the
embedded-signer/upload-certificate comparison all passed. Capacitor runtime
packages were audited and updated to Android/core `8.5.0` and Browser `8.0.4`;
the CLI remains on `8.4.2` because `8.5.0` introduced moderate transitive audit
findings through its optional Xcode tooling.

Local signing material is outside Git and permission-restricted:

```text
/home/tibor/.local/share/propertyquarry/android-signing-v2/propertyquarry-upload.p12
/home/tibor/.local/share/propertyquarry/android-signing-v2/propertyquarry-upload-cert.pem
/home/tibor/.local/share/propertyquarry/android-signing-v2/android-release.env
```

Never print or commit values from `android-release.env`. If rebuilding is
necessary, source it only for the release command:

```bash
source /home/tibor/.local/share/propertyquarry/android-signing-v2/android-release.env
cd /docker/property/mobile
npm run android:release:container
```

## Play Console continuation

Prepared internal-release draft:

```text
https://play.google.com/console/u/0/developers/9007890349240845326/app/4976153363318887490/tracks/4701487190338825843/releases/2/prepare
```

Authenticated BrowserAct session created in this conversation:

```text
Session: pq-play-stealth-auth
Browser ID: 111111582245966456
Browser name: google-play-propertyquarry-release-stealth
Browser type: stealth
```

Use the `browser-act` and `ea-browser-ooda-operator` skills before operating the
session. Do not operate sessions created by another conversation. If the login
has expired, use remote assist for the user to authenticate; never ask for or
enter the user's Google password.

BrowserAct local-storage broken symlinks were moved to:

```text
/home/tibor/.local/share/browseract/.capacity-link-backup-20260806/
```

Real mode-`0700` directories now exist for `kernels`, `profiles`, `browsers`,
and `ffmpeg`. Do not restore the broken links unless their targets exist.

## Exact resume sequence

1. Follow `AGENTS.md` and call vexp `run_pipeline` first for repository work.
2. Wait until after `2026-08-08T12:09:53Z`.
3. Read the `browser-act` and `ea-browser-ooda-operator` skills.
4. Reopen BrowserAct session `pq-play-stealth-auth` and internal release draft
   `2`; obtain a fresh remote-assist login only if necessary.
5. Upload
   `mobile/android/app/build/outputs/bundle/release/app-release.aab`.
6. Require Play to recognize package
   `com.myexternalbrain.propertyquarry`, `versionCode 2`, version `1.1.0`, and
   the active upload signer above.
7. Resolve only internal-track validation issues. Save and roll out draft 2 to
   internal testers only. Do not touch production.
8. Capture the accepted release state and update
   `mobile/build/propertyquarry-google-play-evidence.json` to the accepted
   version-2 artifact.
9. Run `npm run android:release:readiness`; require zero failures or blockers.
10. Run vexp `verify_done`, check Git status, and publish any truthful evidence
    or handoff updates.

## Tester access

The internal tester list contains `tibor.girschele@gmail.com`. The user signed
in as Tibor Girschele, joined the track, installed the current internal build,
and confirmed it launches.

Internal-test opt-in link:

https://play.google.com/apps/internaltest/4701487190338825843

App listing link:

https://play.google.com/store/apps/details?id=com.myexternalbrain.propertyquarry

If Play says the account is not invited, confirm the Play Store is using the
exact invited account above. Do not prefix either URL with a literal `\n` when
sending it through Telegram.

## Audit repairs in the handoff commit

- Updated a stale browser regression to the canonical direct walkthrough route
  `/tours/{slug}/walkthrough`; the endpoint is verified as `video/mp4`.
- Added coherent German and Costa Rican Spanish sign-in copy, including
  provider-click and retry status messages carried through the governed
  localization middleware.
- Added route-level locale regression coverage for the sign-in surface.
- Updated supported Capacitor runtime packages without accepting the CLI audit
  regression.
- Taught the release-readiness gate to verify the active-key/cooldown receipt,
  fail closed on malformed evidence, and report the timed Play restriction as
  an external blocker instead of a local release failure.
- Preserved the preview-build guard that restores signed release bundles after
  Gradle `clean`.

The earlier live-site deployment caveat is closed by the immutable runtime
deployment above. The localized sign-in and bridge bytes are now baked into the
active image; their German failure state was visually verified in BrowserAct
before the immutable rebuild, and the rebuilt image passed current readiness
and public rendering probes.

## Verification completed

- Greenfield browser suite: `127 passed, 1 skipped`.
- Locale contract: `6 passed`.
- Registration and access contracts: `110 passed`.
- Mobile app and release-installation contracts: `44 passed`.
- Mobile web contracts: `11/11 passed`.
- npm audit: `0 vulnerabilities`.
- Android preview build after dependency sync: `242` Gradle tasks, passed.
- Secure sign-in route and mobile contract regression: `30 passed`.
- Secure sign-in mobile web contract: `11/11 passed`.
- Android preview build with lifecycle repair: `242` Gradle tasks, passed.
- Follow-up bridge audit: `30 passed` for identity/mobile routes and `11/11`
  mobile web contracts.
- Android preview build with durable cleanup and bounded share/auth operations:
  `242` Gradle tasks, passed.
- Polished localized bridge contracts: `30 passed`; JavaScript syntax and
  `11/11` mobile web contracts passed.
- Minimal bridge refinement: `30 passed`; JavaScript syntax and `11/11` mobile
  web contracts passed; live German failure state visually verified.
- Public-product premium polish: `884` workspace contracts passed; affected
  browser gate `3 passed, 1 expected skip`; identity/mobile routes `30 passed`;
  `11/11` mobile web contracts passed; live landing and shortlist visually
  verified.
- Public demo conversation: full PropertyQuarry browser gate `129 passed, 1
  skipped`; focused desktop, mobile, and decision follow-up browser gate `3
  passed`; affected API/feedback/shortlist contracts `9 passed`; live readiness,
  rendered controls, overflow, and selected-property answer smoke verified.
- Signed Android release build: `232` Gradle tasks, passed.
- Bundletool: valid.
- Embedded signer: matches the active Play upload certificate.
- Release readiness: `0` failures and exactly `1` external blocker,
  `upload_key_cooldown_until_2026-08-08T12:09:53.000Z`.

The full repository-wide pytest run was stopped after it exposed the stale
walkthrough assertion; the complete affected greenfield file and all targeted
release, localization, registration, and mobile suites were then run to green.

## Evidence and receipts

Ignored local evidence:

```text
mobile/build/propertyquarry-upload-key-activation-receipt.json
mobile/build/propertyquarry-upload-key-cooldown.png
mobile/build/propertyquarry-android-release-evidence.json
mobile/build/propertyquarry-google-play-evidence.json
```

Cooldown screenshot SHA-256:

```text
700fb5c4d9c2be6953e7acf28ce50af2541df9f370967c9de928fcbddc096794
```

`propertyquarry-google-play-evidence.json` intentionally continues to describe
the accepted `1.0` artifact until Play accepts version 2. Do not falsify it in
advance of the successful upload.

The old historical `propertyquarry-android-test-phase-receipt.json` records the
pre-reset missing-key state. Treat it as history, not current release truth.

## Published baseline before this audit

```text
136cc3c5 test(property): align handoff regression contracts
c3311cc8 fix(mobile): preserve release bundle during preview tests
6785350b chore(mobile): advance Android release to 1.1.0
374043fa docs: add Android release session handoff
```

All are on `origin/integration/property-origin-main-20260728`. The audit commit
containing this handoff must also be pushed before declaring repository
publication complete.
