# PropertyQuarry next-session handoff

Updated: 2026-08-11 12:30 UTC

## Mission

The PropertyQuarry `1.1.3` (`versionCode 5`) release is active and available on
the Google Play **internal testing** track. Physical-device telemetry now proves
the repaired flow end to end: Android loaded the runtime contract, started a
fresh Google flow, returned through the registered production callback, opened
the ready native bridge, redeemed the device-bound PKCE handoff exactly once,
and loaded authenticated Search. All expected routes returned HTTP 200 or 303.
The live sign-in incident is closed. Preserve the internal release and its
signed evidence; do not create, edit, promote, or roll out a production release.

The repository audit and repair pass is represented by the published commit
that contains this handoff. Start from that commit and preserve its clean signed
release evidence.

## 2026-08-11 post-success closure audit

A verification-only closure pass after the physical sign-in success found no
regression or remaining auth blocker:

- `propertyquarry-api-live` remained `running healthy` with failing streak `0`,
  no restart, no OOM, and the immutable release image/digest recorded below;
- local and public Android-profile readiness both returned HTTP 200 with
  `postgres_ready:property_search_schema_v20`;
- every auth route since the successful device run occurred exactly once with
  the expected 200/303 result, and no auth route produced a 4xx or 5xx;
- the handoff table contained one consumed physical-device handoff, zero active
  unconsumed handoffs, and only two expired synthetic handoffs awaiting normal
  retention;
- the focused server/native-source suite passed `6/6`, and the mobile web bridge
  suite passed `11/11`;
- the digest-pinned Android preview gate passed all `242` Gradle tasks, including
  native compilation, unit tests, lint, preview APK packaging, and
  instrumentation-test APK packaging;
- public `assetlinks.json` returned HTTP 200 and contained the Play app-signing
  certificate for `com.myexternalbrain.propertyquarry`;
- `/mobile/bridge.js` returned HTTP 200 with UTF-8 and `no-store`, and still
  contained the immutable readiness marker, native auth listener, and exact
  redeem route; and
- the signed AAB still hashes to
  `cb3a90e4c9c337680dfe1e826374ef99df2c7661d56c7d33940d15eb649aa569`,
  matching both release and Play receipts. Release commit `ab0871985` remains an
  ancestor of the current handoff commit, and the only later repository path is
  this handoff document.

No application code, live runtime, Google OAuth client, Play track, signed
artifact, database row, or user session was changed during this closure audit.

## 2026-08-11 physical-device sign-in success

At `2026-08-11 10:20:47 UTC` (`12:20:47 Europe/Vienna`), the installed Android
app began a fresh production run. The complete live sequence was:

```text
2026-08-11 10:20:47 UTC  GET  /mobile/runtime-contract  200
2026-08-11 10:20:48 UTC  GET  /app/search               303
2026-08-11 10:20:56 UTC  GET  /sign-in/google           303
2026-08-11 10:21:00 UTC  GET  /google/callback          303
2026-08-11 10:21:01 UTC  GET  /mobile/auth/bridge       200
2026-08-11 10:21:02 UTC  POST /mobile/auth/redeem       200
2026-08-11 10:21:07 UTC  GET  /app/search               200
```

The database confirms that the handoff issued at `10:21:00.670913 UTC` for the
expected principal was consumed at `10:21:02.466062 UTC`, comfortably before
its three-minute expiry. No code, verifier, challenge, OAuth token, or session
secret was printed or retained in this handoff. The final Search 200 proves the
local authenticated session cookie was installed and accepted after redemption.
This closes the real-device Google sign-in incident; it is no longer merely a
synthetic or server-side result. EA delivered the concise success receipt to
the operator through Telegram as message `5186`.

## 2026-08-11 live OAuth client repair

At `2026-08-11 07:05:14 UTC`, the installed Android 16 build loaded
`/mobile/auth/bridge` and sent `POST /mobile/auth/redeem`. The 400 response came
from a stale, expired local code: the database contained only a handoff issued
on 2026-08-06, already beyond its expiry, and no handoff from the current Google
attempt. This is decisive evidence that release 5 repaired the native-to-web
transport and cleared the stale local payload after its bounded failed redeem.

The app had initiated multiple fresh `/sign-in/google` requests, but production
received no `/google/callback`. A controlled rendered-browser reproduction then
showed Google's `redirect_uri_mismatch` response for the server-configured live
callback. In Google Cloud project `propertyquarry-498318`, OAuth client
`Propertyquarry.com` retained its existing authorized redirect URI
`https://myexternalbrain.com/google/callback` and gained the missing URI:

```text
https://propertyquarry.com/google/callback
```

The change persisted in a fresh Google Cloud Console view and propagated
immediately. A subsequent controlled OAuth flow passed account selection and
2-step verification. Production received `GET /google/callback` with HTTP 303
at `2026-08-11 07:18:48 UTC` and created a new, unconsumed three-minute mobile
handoff for the expected principal. That controlled run deliberately used a
dummy PKCE challenge, so it proved callback and handoff issuance but could not
sign in the physical app. It was allowed to expire. The later physical run
recorded above supplied the device-generated challenge and produced these live
route results:

```text
GET  /sign-in/google      303
GET  /google/callback     303
GET  /mobile/auth/bridge  200
POST /mobile/auth/redeem  200
GET  /app/search          200
```

## 2026-08-11 bridge-readiness repair and internal release 5

The latest build-4 device attempt at `2026-08-11 06:31 UTC` loaded
`/mobile/runtime-contract`, `/mobile/auth/bridge`, and deferred
`/mobile/bridge.js` with HTTP 200, but again produced no
`POST /mobile/auth/redeem`. This narrowed the live failure to a deterministic
ordering race: Android's `onPageFinished` can run before a deferred external
script finishes executing, so build 4 consumed its one-time auth payload and
dispatched the event before the page listener existed.

Commit `ab0871985f8cdfac085370cd7079b5306ba9489e` advances the app to `1.1.3` /
`versionCode 5` and makes the handoff fail closed:

- the bridge installs both auth/share event listeners and only then defines the
  immutable `window.__propertyQuarryNativeBridgeReady` marker;
- Android polls that marker only on the exact trusted bridge page and retains
  pending secrets until readiness is exactly `true`;
- delivery is generation-scoped and bounded to 100 attempts at 100 ms, so page
  changes, activity destruction, or timeouts cannot deliver a stale payload;
- exact scheme, host, port, path, userinfo, query, and fragment checks are
  repeated before consumption and dispatch; and
- the code/PKCE verifier remains private, is consumed once, and is never placed
  in a URL or relaxed into a weaker server contract.

Focused mobile/server contracts passed `6`; mobile web contracts passed
`11/11`; JavaScript syntax and `git diff --check` passed. The pinned preview
pipeline passed `242` Gradle tasks. The signed release pipeline passed `232`
Gradle tasks, bundletool validation, JAR signature validation, and the embedded
signer/upload-certificate comparison.

The matching web bridge was deployed immutably at `2026-08-11 06:43:11 UTC`:

```text
Image tag: propertyquarry-standalone-web-runtime:local-ab0871985f8c
Image ID: sha256:ce42a8316a6e68f8e071675f61cee8a3fa6ef3e8f1d98f5cc12554b9e97e2880
OCI revision: ab0871985f8cdfac085370cd7079b5306ba9489e
Deployment ID: propertyquarry-bridge-ready-ab087198-20260811
Container: propertyquarry-api-live
State after replacement: running healthy, restart count 0
Readiness: postgres_ready:property_search_schema_v20
```

Only the API service was force-recreated with `--no-deps --no-build`; database,
worker, scheduler, renderer, backup, tunnel, network, and volume state was
preserved. The preceding healthy image is retained as
`propertyquarry-standalone-web-runtime:pre-bridge-ready-rollback-20260811`.
A public headless Chromium check observed the readiness marker as `true`,
dispatched a valid-format synthetic native auth event, and observed exactly one
redeem POST containing both `code` and `pkce_verifier`. Its fake code correctly
received the expired-handoff result.

Google Play internal release ID `5` was published at `2026-08-11 06:47 UTC`
(`08:47 Europe/Vienna`). Play reports `5 (1.1.3)` **Available to internal
testers**, zero newly unsupported devices, and the selected `PropertyQuarry
internal` list still contains `tibor.girschele@gmail.com`. The tester opt-in
page recognizes Tibor Girschele and confirms tester status. Production was not
changed. The later physical-device attempt recorded above supplied the missing
end-to-end proof: redeem and authenticated Search both returned HTTP 200. EA
delivered the build-5 update instruction and both clean Play links through
Telegram as message `5183`.

## 2026-08-11 direct native auth handoff and internal release 4

The latest device attempt at `2026-08-11 04:23:17 UTC` again loaded
`/mobile/runtime-contract`, `/mobile/auth/bridge`, and `/mobile/bridge.js` with
HTTP 200 but never called `POST /mobile/auth/redeem`. This disproved the
version-3 document-start injection as a reliable sign-in transport on the real
device.

Commit `fe4f3345d1c48e31f46ce3c210258093cc85a162` advances the app to `1.1.2` /
`versionCode 4` and adds a Capacitor-independent, fail-closed handoff:

- Android dispatches auth data only after an exact
  `https://propertyquarry.com/mobile/auth/bridge` load with matching scheme,
  host, port, path, and no userinfo, query, or fragment.
- The code and PKCE verifier are format-validated and synchronously removed
  from private preferences before their single delivery. Invalid or uncleared
  state is never exposed.
- The bridge accepts the native custom event and performs the existing
  same-origin JSON POST to `/mobile/auth/redeem`; no secret is placed in a URL.
- A failed consumed handoff makes `Try sign-in again` start a fresh external
  Google flow instead of retrying stale state.
- The same exact-origin event lane closes the equivalent pending-share gap.

Focused server/mobile contracts passed `6`; mobile web contracts passed
`11/11`; the pinned preview pipeline passed `242` Gradle tasks, including Java
compilation, unit tests, lint, and instrumentation APK compilation. Added
instrumentation contracts prove valid auth/share payloads are consumed exactly
once. The signed release pipeline passed `232` Gradle tasks, bundletool, JAR
signature validation, and embedded-signer/upload-certificate verification.

The matching web bridge was deployed immutably before the Play upload:

```text
Image tag: propertyquarry-standalone-web-runtime:local-fe4f3345d1c4
Image ID: sha256:4fdbfafa5aef16e0f2f9fb0769b0a7497c47b38339eca84082c7b7fb9deaba0e
OCI revision: fe4f3345d1c48e31f46ce3c210258093cc85a162
Deployment ID: propertyquarry-direct-auth-fe4f3345-20260811
Container: propertyquarry-api-live
State after replacement: running healthy, restart count 0
Readiness: postgres_ready:property_search_schema_v20
```

Only the API service was force-recreated with `--no-deps --no-build`; all
database, worker, scheduler, render, backup, tunnel, network, and volume state
was preserved. The previous healthy image is retained as
`propertyquarry-standalone-web-runtime:pre-direct-auth-rollback-20260811`.
A public headless Chromium check loaded the bridge with HTTP 200, dispatched a
synthetic valid-format native event, and observed exactly one redeem POST whose
JSON contained both `code` and `pkce_verifier`. The synthetic code was not a
real OAuth code and was never expected to authenticate.

Google Play internal release ID `4` was published at `2026-08-11 04:41 UTC`.
Play reports `4 (1.1.2)` **Available to internal testers**, zero newly
unsupported devices, and the selected `PropertyQuarry internal` list still
contains `tibor.girschele@gmail.com`. The tester opt-in page recognized Tibor
Girschele and confirmed tester status. Production was not changed.

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

The Android lifecycle half now reaches testers through internal release 2,
published on 2026-08-09. The deployed web bridge continues to give the older
installed version 1.0 app a bounded retry path, but the decisive remaining
proof is an on-device update to version 2 followed by a fresh secure-sign-in
attempt.

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

## 2026-08-10 Capacitor bridge registration and transport repair

After installing internal version 2, the invited device still showed
`Return to the PropertyQuarry app to finish sign-in` inside the app shell. Live
access logs for the attempt contained `GET /mobile/runtime-contract`,
`GET /mobile/auth/bridge`, and `GET /mobile/bridge.js`, but no
`POST /mobile/auth/redeem`. The exact UI combination proved that the Android
user agent was present while `window.Capacitor.Plugins.PropertyQuarryNative`
was absent.

The published version-2 artifact source commit already registers
`PropertyQuarryNativePlugin` before `BridgeActivity.onCreate`, so no new Android
bundle is required. Capacitor 8.5's runtime only materializes a JavaScript proxy
after `Capacitor.registerPlugin(name)` is called; the remote bridge page had
only inspected the legacy-populated `Capacitor.Plugins` object. Commit
`b7da3047814e4a8eecead95a8199eccc71637c56` now:

- reuses an existing native proxy when present;
- verifies the native plugin header with `isPluginAvailable` before creating a
  proxy; and
- calls `registerPlugin('PropertyQuarryNative')` when the registered Android
  plugin has not yet been materialized for the remote page.

All existing native origin/path allowlists, PKCE verifier handling, durable
cleanup, same-origin redemption, and bounded timeouts remain unchanged. The
focused mobile contract passed `6`; the mobile web suite passed `11/11`; the
wider PropertyQuarry identity/mobile selection passed `263`; JavaScript syntax
and `git diff --check` passed.

The fix was deployed immutably at `2026-08-10 13:32:18 UTC` by recreating only
`propertyquarry-api` with no dependencies or builds:

```text
Image tag: propertyquarry-standalone-web-runtime:local-b7da3047814e
Image ID: sha256:5f2091497df3490e8ef029dd332767d712ca58c411e7b488738b37026aa9e5e6
OCI revision: b7da3047814e4a8eecead95a8199eccc71637c56
Deployment ID: propertyquarry-native-bridge-b7da3047-20260810
Container: propertyquarry-api
State after replacement: running healthy, restart count 0
Bound port: 127.0.0.1:8097 -> 8090/tcp
```

The deployment preflight compared the resolved API environment and all nine
mount targets against the healthy live container. Post-deployment probes
verified PostgreSQL schema-v20 readiness, the baked source mode `0644`, and the
new registration/availability guards through the public Cloudflare path with
HTTP 200, UTF-8, `Cache-Control: no-store`, and cache bypass. The API kept both
networks, all mounts, and its existing runtime environment; no database,
migration, worker, scheduler, render, backup, tunnel, Android, or Play resource
was recreated.

The previous healthy image is retained for local rollback:

```text
Tag: propertyquarry-standalone-web-runtime:pre-native-bridge-rollback-20260810
Image ID: sha256:92974baf94cc172fc9c1b038b634fed50850451ab2ca37cbf78e4a249f25cf9e
```

A factual retry instruction was accepted by Telegram through EA's live
connector binding after deployment. Do not send a duplicate unless the user
asks.

The invited device then reproduced the same screen after the registration-only
deployment. The new attempt again produced two successful runtime-contract and
bridge loads but no `POST /mobile/auth/redeem`, proving that Capacitor's plugin
header remained unavailable rather than exposing an OAuth or network failure.

Commit `d37761920eec3035dd5d3cacd364caefece2bae1` adds a second fail-closed
transport path. When neither `Capacitor.Plugins` nor the guarded
`registerPlugin` call can materialize a proxy, the bridge now calls
`Capacitor.nativePromise` for exactly five existing methods:
`getPendingAuth`, `clearPendingAuth`, `getPendingShare`, `clearPendingShare`,
and `startExternalLogin`. The Java plugin still independently enforces the
trusted PropertyQuarry origin and exact page allowlists, so this bypasses only
the missing JavaScript header—not any security check.

The focused contract passed `6`, JavaScript syntax passed, and the mobile web
suite passed `11/11`. A runtime simulation reproduced the device capability
state (`Plugins` empty, `isPluginAvailable=false`, `nativePromise` available)
and proved the complete sequence: pending auth read, redemption POST, durable
native cleanup, pending-share check, and redirect to `/app/search`.

The raw transport fix was deployed immutably at `2026-08-10 13:43:53 UTC`:

```text
Image tag: propertyquarry-standalone-web-runtime:local-d37761920eec
Image ID: sha256:108349c3676d7c7b6ada576f4bda9ce167b3bea07498eee74f7147f740a971d8
OCI revision: d37761920eec3035dd5d3cacd364caefece2bae1
Deployment ID: propertyquarry-native-promise-d3776192-20260810
Container: propertyquarry-api
State after replacement: running healthy, restart count 0
```

The same environment/mount parity gate passed, public Cloudflare served the
raw fallback with HTTP 200 and `Cache-Control: no-store`, and all non-API
services and Play tracks remained untouched. The immediately preceding healthy
image is retained as
`propertyquarry-standalone-web-runtime:pre-native-promise-rollback-20260810`.
EA sent the fresh device retry instruction to Telegram as message `5165`.

That web-only diagnosis was subsequently disproved by another physical-device
attempt. The remote page had no Capacitor runtime at all, so neither plugin
registration nor the raw native-promise fallback could execute. The statement
above that no new Android bundle was required is retained only as incident
history and is superseded by release 3 below.

## 2026-08-10 Android trusted-origin bridge fix and internal release 3

Capacitor Android 8.5 uses `WebViewCompat.addDocumentStartJavaScript` on modern
WebView, but its allowed-origin set is derived only from the local app URL.
After that registration it nulls the response-time injector. PropertyQuarry's
deliberate local-shell startup and verified remote navigation therefore left
`https://propertyquarry.com` without the native bridge.

Commit `4b76b8f6b4c62442b1a61241765f948fc4477669` fixes the binary boundary without
setting a remote `server.url` or weakening navigation policy:

- `MainActivity` builds the standard Capacitor bridge and the one registered
  `PropertyQuarryNative` plugin export through Capacitor's public `JSExport`
  APIs;
- AndroidX WebKit installs that script at document start for the exact
  `https://propertyquarry.com` origin only;
- the existing native plugin still independently enforces exact trusted paths,
  PKCE state, durable cleanup, and same-origin redemption; and
- versioning advances to `1.1.1` / `versionCode 3`.

The focused server/mobile contract passed `6`; the mobile web suite passed
`11/11`; the pinned preview lane passed unit tests, lint, and APK assembly over
`242` Gradle tasks. The signed release lane passed `232` Gradle tasks plus
bundletool, JAR signature, and embedded-signer/upload-certificate validation.

Google Play internal release ID `3` was published at `2026-08-10 14:04 UTC`.
Play reports the track **Active**, release `3 (1.1.1)` **Available to internal
testers**, and zero newly unsupported devices. The selected `PropertyQuarry
internal` list still contains `tibor.girschele@gmail.com`. The tester opt-in
page recognized that account and offered the test app. Production was not
changed. EA delivered the update instruction and both clean links through the
bound Telegram connector as message `5170`.

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
- Active release: `1.1.3` / `versionCode 5`
- Track: internal testing only
- Internal release ID: `5`
- Track state: **Active**
- Play state: **Available to internal testers**
- Released: `2026-08-11 06:47 UTC` (`08:47 Europe/Vienna`)
- Previous active version `1.1.2` was superseded on the internal track.
- Production rollout changed: **no**

The replacement PropertyQuarry upload certificate is now active in Play:

```text
A8:88:7D:66:41:BF:71:35:E3:74:B0:D8:50:4C:84:1A:F5:D7:26:09:0F:B8:1E:AE:59:67:07:5B:60:81:16:CF
```

Play's post-reset security cooldown ended before the successful upload. The
historical eligibility boundary was:

```text
2026-08-08 12:09:53 UTC
2026-08-08 14:09:53 Europe/Vienna
```

The exact Play message was:

```text
You uploaded an app bundle that is signed with an upload certificate that is not yet valid because it has been recently reset. You will be able to upload app bundles again from 8 Aug 2026, 12:09:53 UTC.
```

No further upload-key reset action is required.

## Signed bundle

```text
Path: mobile/android/app/build/outputs/bundle/release/app-release.aab
Version name: 1.1.3
Version code: 5
SHA-256: cb3a90e4c9c337680dfe1e826374ef99df2c7661d56c7d33940d15eb649aa569
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

## Play Console release result

Published internal release:

```text
https://play.google.com/console/u/0/developers/9007890349240845326/app/4976153363318887490/tracks/4701487190338825843?tab=releases
```

BrowserAct release browser:

```text
Browser ID: 111111582245966456
Browser name: google-play-propertyquarry-release-stealth
Browser type: stealth
Release session: pq-release-build5-20260811 (closed after publication and
tester verification)
```

Use the `browser-act` and `ea-browser-ooda-operator` skills before any later
Console review. Open a new session; do not reuse the closed session name as if
it were a durable handle. If login has expired, use remote assist for the user
to authenticate; never ask for or enter the user's Google password.

BrowserAct local-storage broken symlinks were moved to:

```text
/home/tibor/.local/share/browseract/.capacity-link-backup-20260806/
```

Real mode-`0700` directories now exist for `kernels`, `profiles`, `browsers`,
and `ffmpeg`. Do not restore the broken links unless their targets exist.

## Exact resume sequence

1. Follow `AGENTS.md` and call vexp `run_pipeline` first for repository work.
2. On the invited Android device, open Play Store and update PropertyQuarry to
   `1.1.3` (`versionCode 5`). Wait a few minutes if the update is not yet shown.
3. Fully close and reopen the updated app, then tap `Try sign-in again` and
   repeat Google secure sign-in.
4. Require the flow to leave `Finishing secure sign-in`, redeem the handoff,
   and open the authenticated app surface. Record the device time and visible
   error if it does not.
5. Correlate the attempt with the expected live sequence: Google callback 303,
   bridge GET 200, `POST /mobile/auth/redeem` 200, and authenticated
   `/app/search` 200. If it stops earlier, preserve the device time and logs;
   do not weaken PKCE, session, or origin checks.
6. Do not create another Play release, change the tester list, or promote the
   internal release during this device-validation step. Release 5 is already
   live; only on-device validation remains.

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
- Signed Android release-5 build: `232` Gradle tasks, passed.
- Bundletool: valid.
- Embedded signer: matches the active Play upload certificate.
- Google Play accepted bundle version `5 (1.1.3)` and reports it available to
  internal testers; no form factor lost supported devices.
- The selected `PropertyQuarry internal` tester list contains one user,
  `tibor.girschele@gmail.com`.
- Release readiness at artifact source commit
  `ab0871985f8cdfac085370cd7079b5306ba9489e`: `0` failures, `0` blockers;
  production rollout authorization remains false.
- Running the pre-publication gate at the later repository HEAD intentionally
  reports `source_commit_mismatch`; the published bundle is tied to the clean
  artifact commit above, not to later web and handoff commits.

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
mobile/build/propertyquarry-google-play-internal-v2-20260809.png
mobile/build/propertyquarry-google-play-internal-v3-20260810.png
mobile/build/propertyquarry-google-play-internal-v4-20260811.png
mobile/build/propertyquarry-google-play-internal-v5-20260811.png
```

Cooldown screenshot SHA-256:

```text
700fb5c4d9c2be6953e7acf28ce50af2541df9f370967c9de928fcbddc096794
```

Internal-v2 release screenshot SHA-256:

```text
45c00bb26f65f54eea0d2e5be2766ccb06d9e0aee16c4772f5473db9f03c3665
```

Internal-v3 release/tester screenshot SHA-256:

```text
846215866118c79fc7cdc8dab7b977ff5ab27cdb8bf9b5aa21a7dcec2a546fcb
```

Internal-v4 release/tester screenshot SHA-256:

```text
05c1ff63287afce16ec95034a99ae363e429beba619a23cd35146a3f37f9cf35
```

Internal-v5 release/tester screenshot SHA-256:

```text
7a727e9b72799e290b19e07b1b8a47664faaa1274e2ffeb0efd363b179d3489a
```

`propertyquarry-google-play-evidence.json` now describes the accepted version-5
artifact with SHA-256
`cb3a90e4c9c337680dfe1e826374ef99df2c7661d56c7d33940d15eb649aa569`.
The Play surface still labels the temporary app name as unreviewed; this is not
an internal-track blocker and no production review was requested.

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
