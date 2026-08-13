# PropertyQuarry next-session handoff

Updated: 2026-08-13 09:18 CEST

## Mission

The PropertyQuarry `1.1.3` (`versionCode 5`) release is active on Google Play
**Closed testing - Alpha** in Austria. Earlier physical-device telemetry proved
the repaired flow end to end for the then-deployed head: Android loaded the
runtime contract, started a fresh Google flow, returned through the registered
production callback, opened the ready native bridge, redeemed the device-bound
PKCE handoff exactly once, and loaded authenticated Search. A fresh physical
Android pass for the current exact web head is still required. Preserve the
closed release and its signed evidence; do not create, edit, promote, or roll
out a production release.

The repository audit and repair pass is represented by the published commit
that contains this handoff. Start from that commit and preserve its clean signed
release evidence.

## 2026-08-13 live external-authority receipt and exact Android telemetry

The newest audit findings now have one repeatable, secret-safe live probe:
`scripts/propertyquarry_live_external_authority_probe.py`. It inspects only
allowlisted configuration presence, trusted tool identity, runtime health, and
root-owned launch-authority file posture; it never records credential values.
Its three focused tests and the adjacent Postgres/S3 DR suites pass `108/108`.

The first live receipt was materialized at `2026-08-13T07:16:53Z` as private
mode `0600` file
`state/qa/propertyquarry-live-external-authority-20260813.json`, SHA-256
`9ea2a40c4f76599a58c301ec04be58dc386175d9418f4de679e221b1095999a6`.
It records `secret_values_recorded=false`. The local backup runtime is running
and healthy, and the release-pinned AWS CLI `2.35.16` is attested at SHA-256
`d17130561271a8c117f517c03bcd867fd8525408c999558149dc8f2f8f9b1d3d`.
The following gates are still absent and therefore fail closed:

- an approved external GPG encryption recipient;
- a usable scoped AWS identity;
- a configured versioned S3 target with Compliance Object Lock retention;
- a disposable restore database plus `pg_dump`, `pg_restore`, and `psql`
  toolchain;
- both signed, root-owned public-launch authority files;
- the ten required same-principal live billing fields; and
- the five required external FlipLink fields.

The live probe consequently preserves billing as unavailable, FlipLink as
`local_only`, public launch as unavailable, and DR as unproven off-host. These
are exact authority/configuration blockers; do not replace them with guessed
credentials or weaker storage claims.

Fresh log telemetry from the exact deployed candidate
`290e4e7efb473f0602ab29f2a34e85c616e3e331` was checked from its
`2026-08-13T06:22:30Z` deployment through this audit. It contains 49
authenticated Search 200 responses, one Search 303 boundary, and 13 expected
Search 401 probes, all bound to the exact candidate and web image. It contains
no `/mobile/runtime-contract`, `/sign-in/google`, `/google/callback`,
`/mobile/auth/bridge`, or `/mobile/auth/redeem` event. This is not a live auth
failure: it proves that no fresh installed-app attempt occurred after this
candidate was deployed. A physical tester must open the current installed app,
complete Google sign-in, and reach Search once; only the resulting ordered
runtime-contract/callback/bridge/redeem/Search-200 telemetry can close that
exact-candidate Android gate. The last Play Console read remains `6/12` opted-in
testers, so six additional distinct real testers and the continuous 14-day
window remain external production prerequisites.

## 2026-08-13 fresh live customer and LTD proof on deployed candidate

The deployed source remains
`290e4e7efb473f0602ab29f2a34e85c616e3e331` in web image
`sha256:7aa1390f615193eafefd52a2f2a6f217ce193ba35427a75c6be2bd1f6089cb53`.
Two fresh customer-bound proofs now close the remaining exact-candidate browser,
opportunity, brief, and live-LTD evidence gaps without changing the runtime.

The full protected mobile browser matrix passed at
`2026-08-13T06:45:32.286016Z`: all 16 configured customer routes in Chromium,
Firefox, and WebKit at 390x844 and 412x915, for `96/96` real Playwright samples.
There were zero failures, missing engines, missing samples, or static fallbacks;
the concrete protected research-detail route remained authenticated. Billing
returned the expected six fail-closed HTTP 503 responses for the Free persona.
Receipt `state/qa/propertyquarry-live-browser-all-20260813-exact-290e4e7e.json`
has SHA-256
`9f24db275cde0937009dc9b67b34047fee6add88ae26949acd11f15e76e281af`.

Authenticated reviewer `play-reviewer@propertyquarry.com` then used the rendered
six-step search flow. Steps 1-5 exposed `Next`; step 6 replaced it with the
single `Launch search` action. The browser persisted the Vienna/AT profile with
HTTP 200 and created run `6e868ff69cd44a3e8ddf3d586d55cfa9` with HTTP 202.
All three selected Austrian marketplaces completed in one durable queue attempt:
30 raw listings, 6 reviewed and ranked candidates, 6 persisted assessments, 6
persisted opportunities, and zero opportunity-persistence failures. The first
shortlist was ready in 11,411.29 ms and the run completed in 14,197.12 ms.

The same signed-in principal generated private brief
`sum_777228978eca4947a12a5293f7772217` (event
`evt_238d1d4d5a8c4827a41b93e70773bd83`) for top opportunity
`property_opportunity:a5e22c95854e520ef1f437395ecc173590426f2aef115d9d12a201f3ca110327`.
PostgreSQL read-back proves 22 lines and 720 characters with recommendation,
fit, confidence, predicted reaction, reasons, trade-offs, verification steps,
and a validated HTTPS listing link. Publication remains correctly `local_only`
and `not_published`. Receipt
`state/qa/propertyquarry-real-signed-in-search-290e4e7e-20260813.json` has
SHA-256 `59b1704dc68321b320407fc650cde940ef4e8178d7d43e7a55f2e02ada4fe900`.

Finally, that exact opportunity executed one fresh principal-bound 1min.AI
`image_generate` call through the durable worker. Generation
`a4f661939386443395ace578f45ce361` completed after exactly one attempt with
model `gpt-image-1-mini`; the receipt is `verified`, has proof scope
`provider_call`, and is bound to the requesting principal. The provider prompt
used privacy scope `no_listing_or_customer_identifiers_sent`. The private
first-party PNG is 1024x1024 and 1,305,522 bytes with SHA-256
`a3dbd34e02a791ea427a3b921aacf4c2292dc85a7aaf3fc38c5080ca2ea66fcf`.
An authenticated browser fetched the materialized asset with HTTP 200,
`image/png`, `Cache-Control: private, no-store`, and independently reproduced
the exact byte count and SHA-256. Publication remains `not_published` with no
external publication claim.

The worker-side aggregate snapshot at `2026-08-13T07:03:34.866006Z` still
reports 70 configured 1min slots, 25 successful formal probes, 45 depleted
probes, 26 live-dispatchable slots, and 2 ready slots. The generic API-process
probe correctly sees no credentials because API and worker duties are
separated; do not copy worker credentials into the API. Receipt
`state/qa/propertyquarry-live-ltd-cover-290e4e7e-20260813.json` has SHA-256
`9e54ea776295a37feba859ae8e921113e139bcb0eee4f85fbc1e4eb75b7a09e1`.

No source repair was required by these fresh proofs. A read-only Play Console
check at `2026-08-13T07:09:09Z` shows Closed testing still active and Production
inactive, with `6/12` testers currently opted in. The remaining gates are a
physical Android callback/redeem/Search pass on this exact web candidate; 6
additional distinct real Play testers plus the 14-day rule and explicit
production-launch authority; dedicated same-principal Live billing credentials
and admission; real external FlipLink credentials, privacy/export approval,
publication, and read-back (or continued honest `local_only` behavior); and an
approved encryption recipient, scoped off-host identity, immutable target,
exact-version read-back, and disposable restore proof. Do not mark the
long-running goal complete while any gate remains.

## 2026-08-13 audit closure, canonical-main merge, and live deployment

GitHub pull request [#4](https://github.com/ArchonMegalon/propertyquarry/pull/4)
merged the verified integration history into protected `main` at
`bbef2d6fec67722d559df4ee2cda1e093d714a17`. Local `main` and `origin/main`
were synchronized to that merge before deployment. The immutable runtime source
candidate is `290e4e7efb473f0602ab29f2a34e85c616e3e331`; its release artifact set is
`propertyquarry-generated-release-artifacts-v1@sha256:d230f659b9e14a3e0dd8aaf5fe6d6eaa40eaf2a04d7935c71cb9affe829a20df`.

The governed local Docker deployment passed at `2026-08-13T06:22:30Z` with no
failures and no secret values recorded:

- web image: `sha256:7aa1390f615193eafefd52a2f2a6f217ce193ba35427a75c6be2bd1f6089cb53`
- render image: `sha256:41394141d94d01d805a945a9c8d7e1e8bbbaf3499039c238bc49c478fa997fd4`
- deployment receipt: `state/release/propertyquarry-local-deployment.v1.json`
  (`sha256:1c815e0cd9a124e2f20f7fee6478fa13c55d9f3d1dd874c187f9d8f59bcb1723`)
- security receipt: `state/artifacts/property-security-posture-current.json`
  (`status=pass`, zero failures, candidate and web-image bound;
  `sha256:36d911cda2bf95689d3369bd7237846d2b4ae1d99f4ce90f9f13504dc555228b`)

Live `/version` reports the exact candidate, exact web image, complete release
manifest, and the expected artifact set. Live `/health/ready` reports
`postgres_ready:property_search_schema_v20`. Anonymous `/app/search` returns
the expected authentication boundary rather than exposing a customer workspace.

The audit repair set now includes: default-deny private showcase access; owner-
only local dump permissions; honest LTD provider-evidence versus customer-
integration semantics; principal-and-service-bound BrowserAct executability;
Austria-only customer geography; one Free/Plus/Agent billing vocabulary; local
packet and decision-tray copy; an explicit viewing-request action; a non-stale
handoff pointer; and the frozen Play package identity exception. Affected suites,
93-test focused closure, 282-test broader closure, generated-artifact
reproduction, canonical release preflight, merge, deployment, and live probes
all passed.

Do not mark the long-running goal complete. Play remains Closed testing in
Austria at the last proven `1/12` opt-in count; production still needs 12 real
testers for 14 continuous days and explicit Play launch authority. A fresh
physical Android callback/redeem/Search cycle for this exact web candidate is
still required. Dedicated same-principal live billing, external FlipLink
publication, encrypted immutable off-host restore authority, and public-launch
authority remain absent and fail closed. Other markets, overlays, customer LTD
completions, and whole-project Gold remain backlog until exact live evidence
promotes them.

## 2026-08-13 previous actionable-assessment candidate and test closure

This earlier candidate was superseded by the audit-closure deployment recorded
above. Its published and deployed source candidate at the time was
`0a44ea202695163cf00dc8807c69d55dd0a561fc`
(`fix(property): preserve actionable assessment briefs`). Generating a brief
now reuses a ready durable opportunity instead of rematerializing and
overwriting its richer assessment. Customer briefs retain known facts such as
`heating type`, label the predicted reaction, and continue to expose
recommendation, fit, confidence, reasons, trade-offs, verification steps, and
the validated listing link. Source proof was published in `864f5c7a`, the
flagship/pulse binding in `e9b003e2`, and the exact evidence envelope in
`4fb376e0e8113fe7383e765a6190b7b6aa902bb4`. The release artifact-set identity
is
`propertyquarry-generated-release-artifacts-v1@sha256:29f2b7b8079d68f3cfc651f717189a0566ac3812de48b343555b341b784be804`.

The affected opportunity suites pass `545 passed, 4 skipped`; the focused five
tests, exact compilation, generated-artifact sandbox reproduction, detached
manifest verification, 16/16 browser materializer, 8/8 journey matrix, and all
source lanes also pass. Fresh deployed artifact
`sum_1833a5357cd24193bc4d56a00ce2d760` was generated for durable Austrian run
`0d39c56749b04ec795302ad5e1ab6023`. It is 738 characters over 22 lines, keeps
the numeric fit/confidence and five unknowns, includes the predicted-reaction
and verify sections plus HTTP listing link, and was independently read back
from PostgreSQL with `generation_mode=local_opportunity_brief`,
`generation_basis=durable_preference_assessment`,
`publication_mode=local_only`, and `external_status=not_published`.

The exact candidate is healthy in web image
`sha256:b98bfbb246fc51812fc60d1bc1121c86f453339886b1a993b0b56f3518894cff`;
render tools remain
`sha256:98dfcedb74ab97862a88cc985ee7a795696cbc6f080793c37883054aec33b79e`.
Deployment receipt `state/release/propertyquarry-local-deployment.v1.json`,
SHA-256 `2fbf587d18c820372863d7bec77c69fb7be69f3789d398eff2d5c2ad6c50b8fe`,
records `passed=true`, runtime candidate `0a44ea202695...`, envelope
`4fb376e0e811...`, and zero failures.

Fresh exact live receipt
`state/qa/propertyquarry-live-browser-all-20260813-exact-0a44ea20-pass.json`,
SHA-256 `d93161eb41e93bca6a6ad1212f19c9f7d877de913dcf244b8332834243ec102a`,
records `96/96` real Playwright samples and zero failures at
`2026-08-13T05:15:41.936374Z`: 16 routes in Chromium, Firefox, and WebKit at
390x844 and 412x915, with no missing engines, samples, surfaces, or static
fallbacks. The earlier single WebKit 390x844 Search measurement of 40 px did
not reproduce in three focused release-probe runs or the full repeat; the exact
row now measures 48 px actions and 44 px primary targets. No threshold was
weakened and no speculative CSS change was made. Billing remains the honest
Free-persona fail-closed HTTP 503 contract.

Supplementary Android lab proof is current for source `0a44ea...` (not a claim
of fresh physical-handset proof). The pinned preview build completed all 242
Gradle tasks. Preview APK SHA-256 is
`ecf09e791253b7be30eda0b52be8dd73cb5f040703deb46bae2d5b3e77d9690c` and
instrumentation APK SHA-256 is
`8e334ec637ac774781c93bddb03031d66012d1f6be0ced372b43dee6a3c34099`.
The preview installed and launched in the local Android emulator, loaded the
production runtime contract, opened the real Google authorization page for
propertyquarry.com, and the four installed instrumentation tests passed. No
Google account was entered in the lab, so a current callback/redeem/Search-200
cycle on a physical handset remains required.

The manual group handoff is complete. Google Groups showed six members at the
last observation; identities belong in the ignored operator ledger, not this
tracked handoff. Membership makes those accounts eligible to visit the Play
opt-in URL; it does not prove that they opted in. The last confirmed Play
production-access count remains 1/12, followed by the required 14 continuous
days once 12 real testers are enrolled.

Fresh launch-room receipt
`state/qa/propertyquarry-launch-room-20260813-final.json`, SHA-256
`154bfcb12fc3a17291bffc9bbc8a17258d3562ddeb04301125ea599df82015ef`,
records `local_runtime_ready=true`, exact candidate/envelope bindings, and
`production_launch_ready=false`. Its remaining blockers are exactly:

- `external_public_launch_authority_receipt_missing`
- `google_play_public_launch_authority_unverified`
- `paid_billing_safe_handoff_authority_unverified`
- `encrypted_off_host_disaster_recovery_authority_unverified`

Do not mark the long-running goal complete: the fresh physical Android cycle,
11 additional real Play opt-ins plus 14 days, dedicated live billing canary,
and encrypted immutable off-host restore authority are still external gaps.
External FlipLink publication may remain closed while the useful generator is
honestly `local_only`.

## 2026-08-13 actionable opportunity briefs and exact deployed proof

Published source candidate `8d962091570819c4746b6c6c2816642dde5f1c6c`
(`feat(property): generate actionable opportunity briefs`) replaces the former
one-line local summary with a bounded, escaped customer brief. The default
local artifact now contains a heading, recommendation, numeric preference fit,
confidence, predicted reaction, fit reasons, trade-offs, optional blocking
constraints, explicit next checks, and a validated HTTP(S) listing link. URL
validation rejects credentials, malformed ports, whitespace/control injection,
Markdown delimiters, missing hosts, and non-HTTP schemes. Publication remains
honestly `local_only` / `not_published`; this is not a claim of external
FlipLink authority.

The focused and affected suites pass `25/25`, exact Python compilation passes,
and the generated release artifacts exactly match their committed bytes. The
release artifact-set identity is
`propertyquarry-generated-release-artifacts-v1@sha256:9476cb873a0dfbc2a77a1b2c9f736e9d3b5ea20d35cd1ad8b2142f246a838751`.
The published evidence envelope head is
`9b97491309cdfc41c49b9e9bdabac16c3fec0999`.

The exact candidate is deployed and healthy. Local deployment receipt
`state/release/propertyquarry-local-deployment.v1.json`, SHA-256
`482a501fd64c4a4f77cca9dcbbaa84c73e73ceb02f5ebb6c5bf7065a36eeaea9`,
records `passed=true`, runtime candidate `8d962091570819c4746b6c6c2816642dde5f1c6c`,
and envelope head `9b97491309cdfc41c49b9e9bdabac16c3fec0999`, with no failures. The API,
worker, scheduler, and migration service run exact web image
`sha256:96703a762c12c8161b64f9edc19173e714c6a7d5e5618765e04271b62eeb769f`;
render tools run
`sha256:98dfcedb74ab97862a88cc985ee7a795696cbc6f080793c37883054aec33b79e`.

Fresh artifact `sum_ee1cb30c0b2d4f59a547ebe9db384975` was generated through the
deployed route for durable Austrian run `0d39c56749b04ec795302ad5e1ab6023`.
It is 714 characters over 22 lines and contains every actionable section plus
the listing link. A separate process read it back from the PostgreSQL-backed
event repository with `generation_mode=local_opportunity_brief`,
`publication_mode=local_only`, `external_status=not_published`, and a durable
preference-assessment generation basis. All ten ranked candidates in that run
have persisted numeric fit/confidence, recommendation, and unknowns; the tenth
assessment was written later than the initial audit timestamp and is present.

Fresh deployed receipt
`state/qa/propertyquarry-live-browser-all-20260813-exact-8d962-pass.json`,
SHA-256 `621fc879318e4c8af8bcc3c8127752e93d7b5d2633609e745be930815ada0ac5`,
records `96/96` real Playwright samples and zero failures at
`2026-08-13T04:14:31.984602Z`: all 16 configured customer routes in Chromium,
Firefox, and WebKit at 390x844 and 412x915. It has no missing engines, samples,
surfaces, or static fallbacks. The allowlisted release-probe research detail
rendered the full authenticated workspace in all six combinations. A prior
attempt against a real run owned by another principal correctly redirected to
sign-in in every engine; that receipt is negative tenant-isolation evidence,
not an app regression, and no authentication boundary was weakened. Billing
returned the intentional fail-closed HTTP 503 in all six samples and passed the
Free-plan compatibility contract.

The public-join tester group still needs its pending five-address direct-add
reCAPTCHA completed. BrowserAct session `pq-goal-audit-20260813` is locked for
manual handoff; the operator received the live assist link in Telegram message
`5208`. Do not issue any command in that session until the operator explicitly
replies `done`, then verify the members rather than assuming the submission
landed. Play still needs eleven more real opt-ins and fourteen continuous days.
A fresh physical Android sign-in on this exact candidate, dedicated live
billing authority and canary, external FlipLink authority if desired, and
encrypted immutable off-host restore authority also remain open. The global
release preflight therefore remains honestly blocked; do not mark the
long-running goal complete.

## 2026-08-13 LTD truth separation and exact-image browser proof

Commit `8ae3bb9a` is published on
`integration/property-origin-main-20260728`. It closes the audit defect where
catalog presence, a callable local contract, a BrowserAct template, or a manual
seed could be mistaken for a live provider integration. The deployed LTD
profile now reports `evidence_status`, `verification_source`, `last_verified`,
`live_evidence_verified`, and
`propertyquarry_customer_integration_verified` separately from whether a local
action contract is executable.

The exact deployed profile truth is:

- 1min.AI is Tier 1 `live_provider_evidence`; its worker health probe and
  principal-bound provider receipt prove both live evidence and a real
  PropertyQuarry customer integration.
- AI Magicx is Tier 2 `provider_contract_available`; Crezlo is Tier 2
  `account_discovery_contract_available`; neither has live evidence or a
  PropertyQuarry customer integration.
- FlipLink is Tier 2 `runtime_contract_available`, with
  `reported_owned_unconfigured`; it is not a managed/live provider and all
  customer publication remains honestly `local_only` / `not_published`.
- Internxt is Tier 2 `live_account_evidence` because its authenticated rclone
  principal is verified. That is storage-account evidence only, not a
  PropertyQuarry customer integration or compliant DR.
- PayPal is Tier 2 `account_discovery_contract_available` with
  `sandbox_identity_isolated`; it has no dedicated PropertyQuarry Live
  identity, no live integration, and no customer billing authority.
- PayFunnels remains exactly `unconfigured_external_authority`; contract tests
  do not convert it into a live service.

`LTDs.md` now records those same boundaries. AI Magicx and Crezlo were demoted
from unsupported Tier 1 claims, PayPal's generic Sandbox identity is recorded
as isolated rather than as a PropertyQuarry credential, and Internxt's real
authenticated account is recorded without inflating it into DR or customer
integration. The critical verifier passes with an explicit
`not_live_integration_gate`. The flagship verifier passes all nine exact
posture rows while counting only five as live service/account/provider evidence
and four as contract-only, auxiliary, or unconfigured. Of those five, only the
1min receipt is admitted as a verified PropertyQuarry customer integration.
The focused catalog/API/verifier/runtime/tool suite passes `203/203`; exact
Python compilation and `git diff --check` pass.

The repair is live in web image
`sha256:83a9d4ab984cbdeedb22e5080fe3e1af6e98ed7ebe58bb2104029d752bd2761b`.
Fresh exact-image receipt
`state/qa/propertyquarry-live-browser-all-20260813-ltd-truth-exact.json`,
SHA-256 `3eacf3c9753998e2542f64516867642b6b7bae692d93a73e7bdd4ca78316b76c`,
records `96/96` real Playwright samples and zero failures at
`2026-08-12T22:22:39.231371Z`: all 16 configured customer routes in Chromium,
Firefox, and WebKit at 390x844 and 412x915. There are no missing engines,
samples, or static fallbacks. Ordinary surfaces returned HTTP 200; all six
billing samples returned the expected fail-closed HTTP 503 and passed the Free
plan compatibility contract.

The remaining blockers are external and unchanged: eleven more distinct real
Play testers plus Play's fourteen-day continuous enrollment requirement; a
fresh physical Android sign-in pass for this exact web head; dedicated Live
billing-provider credentials plus the complete same-principal canary and
admission; and an approved external encryption recipient, scoped AWS identity,
COMPLIANCE-locked S3 target, exact-version read-back, and disposable restore
proof. FlipLink may remain closed as an honest `local_only` generator unless
external publication authority is supplied. Do not mark the long-running goal
complete while any of these gates remains.

## 2026-08-13 live external-authority refresh

A secret-safe deployed-runtime audit at `2026-08-12T22:28:18Z` found no new
authority after the exact-image deployment. The preceding six hours contained
zero requests to `/mobile/runtime-contract`, `/sign-in/google`,
`/google/callback`, `/mobile/auth/bridge`, or `/mobile/auth/redeem`. The 48
authenticated `/app/search` reads in that window were the browser matrix, not a
native-device flow. A fresh physical Android sign-in therefore remains
unproven for this exact web head.

The API still has the official PayPal Live origin but no client ID or secret.
Its PayFunnels API key, webhook secret, and both paid checkout URLs remain
absent. Every field of the exact-release paid-billing safe-handoff admission is
empty. The API, worker, and scheduler also have no FlipLink login, password,
BrowserAct enablement, or webhook secret. Billing must remain HTTP 503 and
FlipLink must remain `local_only`; no provider-side state was changed.

The host still has zero GPG public recipients, zero AWS credential/profile
authority, and no S3 bucket, key prefix, Object Lock duration, or backup
encryption recipient in the deployed services. This is current evidence that
the encrypted immutable restore path cannot be executed safely; it is not a
claim that the plain authenticated rclone remotes disappeared.

Google Play was re-read through the dedicated authenticated PropertyQuarry
browser without making a Console change. The tester invitation still says
`You are a tester` for the enrolled operator account. Closed Alpha remains
active with release `5 (1.1.3)`, one country/region, and the six-address
`PropertyQuarry internal` list. The production-access dashboard still reports
exactly `1 tester currently opted in`; it requires at least 12 opted-in testers
and then at least 14 continuous days. The Open testing page still states that
Open testing is available only after Production access. The isolated browser
session was closed after the read.

## 2026-08-13 public-join Austria tester group published

The manual Google reCAPTCHA was completed through the dedicated BrowserAct
handoff and the group `PropertyQuarry Austria Testers` now exists at
`propertyquarry-austria-testers@googlegroups.com`. Its selected settings were
read back from Google Groups: **Who can see the group** is `Anyone on the web`
and **Who can join the group** is `Anyone on the web can join`. It currently has
one member. The clean public join URL is:

https://groups.google.com/g/propertyquarry-austria-testers

Play Console Closed testing - Alpha was changed from the six-address
`PropertyQuarry internal` email list to that Google Group. The exact change was
`Set testers to be managed by Google Groups:
propertyquarry-austria-testers@googlegroups.com`. It was sent to Google for
review, approved, and published on 13 August 2026. Publishing overview reports
`Last published on 13 August 2026` and `App update published`; managed
publishing remains off. A post-publication read of the Alpha tester tab shows
the group as the current eligibility source, and the web opt-in page reports
`You are a tester` for the enrolled group member. Only Google accounts that
have joined the group can opt in; public visibility of the URL alone does not
count toward the 12-tester production-access requirement.

Play Console exposed and verified these exact tester URLs:

https://play.google.com/apps/testing/com.myexternalbrain.propertyquarry

https://play.google.com/store/apps/details?id=com.myexternalbrain.propertyquarry

The group URL, web opt-in URL, and Android app URL were delivered to the
operator through `tibor_concierge_bot` as Telegram message `5206`, with each
URL on its own line and no literal `/n` prefix. After Google published the
change, final live-status message `5207` delivered the same clean URLs. The
preceding manual-CAPTCHA handoff was Telegram message `5204`. Do not repeat
group creation or CAPTCHA; next session should verify the group-backed opt-in
path with a distinct tester account before counting it.

## 2026-08-12 current-head opportunity and DR revalidation

The Austria opportunity claim was re-read directly from deployed PostgreSQL at
`2026-08-12T21:29Z` against source head
`60823de164644a74c6e0cebfcb349c57fcd3841b` and web image
`sha256:c3641c97aed096adb9a2792c9a332273aaf5627c951ce5f412d0050808cd10fa`.
Real reviewer-principal run `0d39c56749b04ec795302ad5e1ab6023` remains
`processed`, has 10 ranked Austrian candidates, 10 persisted opportunity
assessments, and zero persistence failures. The named assessment for
`property-scout:1355793819` remains principal- and run-scoped in
`preference_decision_assessments` with a numeric fit score and confidence,
recommendation, predicted reaction, one match reason, and five explicit
unknowns.

The useful brief is also durable in the deployed database; it is not expected
in the generic kernel `artifacts` table. Artifact
`sum_f07010e1bf9443d68817e1316ce801c6` resolves in
`property_packet_publication_events` as exactly one
`property_summary_artifact_generated` event for the same principal. Its stored
body is 220 characters, its generation mode is `local_opportunity_brief`, and
its publication fields remain honestly `local_only` and `not_published`.
Preserve this event repository as the authoritative summary-artifact store.

The newest local database dump remains
`propertyquarry-20260812T092158Z.dump`, 42,344,555 bytes. At
`2026-08-12T22:06Z`, its recorded SHA-256 sidecar passed `sha256sum -c` from
the correct `/backups` working directory and its custom-format catalog passed
`pg_restore --list`. This proves local artifact integrity only. The newer
off-host authority section below supersedes the earlier claim that this host
has no off-host storage or trusted AWS CLI.

## 2026-08-13 off-host DR authority correction

This host does have authenticated off-host storage: rclone mounts exist for
pCloud, OneDrive, and Internxt. pCloud reports about 18.69 TB total and 8.28 TB
free. A bounded write/read/delete canary at
`pcloud:PropertyQuarry/DR-authority-probes/2026-08-13T0005CEST-write-read-canary.txt`
proved the pCloud principal writable at `2026-08-12T22:05:08Z`: the 66-byte
provider object read back with the exact expected SHA-256
`62fd87a38c7a2a58cf3c5776871b0667794894c6a4b2787b47843446cfb2abc8`.
The exact canary was then deleted and its absence verified. No database bytes,
credentials, customer data, or release artifact were uploaded.

This does **not** satisfy the launch DR contract. All three configured rclone
backends are plain provider remotes (`pcloud`, `onedrive`, and `internxt`), not
an encrypted `crypt` remote. pCloud exposes SHA-256 and normal copy/move/delete
operations, but no compliance retention tier, immutable provider version ID,
Object Lock, or metadata contract matching
`propertyquarry.off_host_retrieval.v2`. The host GPG keyring still contains zero
public recipients, so creating an encrypted artifact now would either be
unrecoverable after host loss or would invent an unapproved recovery secret.
Do not upload the plaintext dump or count these sync mounts as launch DR.

The tracked AWS CLI release pin is now genuinely `CONFIGURED`: executable
`aws-cli/2.35.16` exists at the pinned path, its SHA-256 matches
`d17130561271a8c117f517c03bcd867fd8525408c999558149dc8f2f8f9b1d3d`,
and the minimal-environment version probe passes. The command is deliberately
outside the ordinary host `PATH`, and the backup container still does not carry
it. What remains external is the provider authority: the host has no AWS
environment credentials or profile, the pinned CLI's read-only STS probe fails
`NoCredentials`, and no PropertyQuarry S3 bucket, region, key prefix, or Object
Lock duration is configured. The public-launch authority and runtime trust
store files also remain absent. A compliant run still requires an externally
held encryption recipient plus scoped AWS credentials for a versioned S3
bucket with COMPLIANCE Object Lock, followed by exact-version provider
read-back and a disposable restore receipt.

Do not use `rclone config redacted` for future audits: the Internxt backend did
not redact an embedded access-token field. The observed access token was
already expired and is not recorded in this handoff; inspect remote types via
`listremotes` and `backend features` instead.

## 2026-08-12 Elisabeth lifetime Agent entitlement applied

The previously requested top-tier lifetime grant for
`elisabeth.girschele@gmail.com` had not actually been applied. The repository's
governed `scripts/propertyquarry_lifetime_agent_entitlement.py` plan resolved
exactly one live principal and reported a real change. The apply path then
created the required private rollback snapshot before its compare-and-swap
update and verified both durable commercial projections before commit.

Receipt `state/qa/propertyquarry-elisabeth-lifetime-agent-applied-20260812.json`,
SHA-256 `0199d24fc4e746a0ae9eedb3e9354703ebf39421eb190e2280fc5ca163a0fb07`,
records `status=applied`, one resolved principal, `plan_key=agent`,
`kind=lifetime`, and `active_until=2999-01-01T00:00:00+00:00`. The change is
marked `verified=true` and made zero provider calls. The receipt contains only
email/principal digests, not the target email or raw principal identifier.

The rollback snapshot is
`state/runtime/propertyquarry-elisabeth-lifetime-agent-20260812.rollback.private.json`,
mode `0600`, SHA-256
`b6ced20d359e97fb5d0cd739e8aee38cc54308c6e3d7d88db802c0d924075798`.
Keep it private. An independent post-commit plan resolved the same principal,
produced identical before/after digests, and reported `changed=false`; the
grant is therefore live and idempotent. The focused entitlement suite passes
`18/18`.

The authenticated Play Console read at `2026-08-12T21:49Z` still reports
Closed testing active, Production and Open testing inactive, and exactly
`1 tester currently opted in` of the required 12. No Play setting was changed.
The Open testing page was also read directly: it is explicitly locked until
this personal developer account has Production access, so an Austria open link
cannot bypass the closed-test requirement. The isolated BrowserAct session was
closed after the read. Fresh Android telemetry remained absent and this host
still had no `adb`; the physical-device boundary below is unchanged.

## 2026-08-12 billing-principal isolation and environment binding

A secret-safe inventory of every running Docker container found only one
PayPal client/secret pair: the same generic EA/other-product identity had been
projected into PropertyQuarry. PostgreSQL `provider_bindings` contains no
PayPal principal binding and no alternative Live PayPal identity exists in the
current runtime. A fresh non-transactional OAuth probe classified the shared
identity as **Sandbox**: the official Live endpoint returned HTTP 401 and no
token, while the official Sandbox endpoint returned HTTP 200, a token with 31
scopes, and a 30,965-second lifetime. No order, capture, payment, webhook
mutation, or checkout activation occurred in either probe.

Commit `c503fa9f65b242695c6446d6aef2c36128dc0b40` removes that ambient
credential inheritance. PropertyQuarry Compose now accepts PayPal material
only through the dedicated external inputs
`PROPERTYQUARRY_PAYPAL_LIVE_CLIENT_ID` and
`PROPERTYQUARRY_PAYPAL_LIVE_SECRET`, maps them only into the API's conventional
PayPal variables, and hard-pins `PAYPAL_API_BASE=https://api-m.paypal.com`.
Generic `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`, or a Sandbox base cannot flow into
PropertyQuarry. The current live container therefore reports both credential
fields absent, environment `live`, `paypal_configured=false`, and
`paid_billing_safe_handoff_configured(provider="paypal")=false`.

Commit `5ffed42cca8f1a8e298c4415c8ce7e80272da77e` applies the same boundary
to PayFunnels. The runtime inventory found no PayFunnels principal binding,
API key, or paid-plan URL, but PropertyQuarry was still receiving EA's generic
webhook secret. Compose now accepts the API key, webhook secret, and Plus/Agent
checkout URLs only through dedicated `PROPERTYQUARRY_PAYFUNNELS_LIVE_*`
inputs, while `PAYFUNNELS_API_BASE` is hard-pinned to
`https://api.payfunnels.com`. Generic EA `PAYFUNNELS_*` values cannot flow into
PropertyQuarry. The deployed API now reports its API key, webhook secret, and
both checkout URLs absent; both paid plans and the PayFunnels safe handoff
report false.

The admission boundary now prevents this mismatch from becoming a customer
incident. `property_billing.py` accepts only the two exact official PayPal API
origins, identifies them as `live` or `sandbox`, and permits customer checkout
only on the Live origin. The external safe-handoff admission must additionally
set `PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PROVIDER_ENVIRONMENT=live`;
Sandbox evidence can never satisfy that field. Compose forwards the new
non-secret binding. The missing/invalid admission still fails closed before any
provider call, and the deployed customer billing surface must remain HTTP 503
until valid Live credentials and the complete same-principal canary exist.

The final post-isolation Compose and billing regression set passes `17/17`;
the earlier broader checkout, entitlement, and Compose set passes `33/33`.
Exact Python compilation and `git diff --check` pass. The live credential
probes are provider-health evidence only, not the required checkout/webhook/
entitlement/cancellation canary.

## 2026-08-12 exact-image browser and blocker refresh

The final billing-principal-isolated exact-image matrix is complete. Receipt
`state/qa/propertyquarry-live-browser-all-20260812-billing-principals-isolated-exact.json`,
SHA-256 `8201f6472a06ec5d3b90afd9092c71629eaaaa772637db6a8f74030663551f33`,
records `96/96` real Playwright samples and zero failures at
`2026-08-12T21:59:49.667732Z`: all 16 configured customer routes in Chromium,
Firefox, and WebKit at 390x844 and 412x915. The receipt has no missing engine,
sample, customer surface, or static fallback and includes the concrete saved
research-detail route in redacted form. Every ordinary customer page returned
HTTP 200. All six billing samples returned the expected fail-closed HTTP 503
and passed the explicit Free-plan compatibility contract.

This proof ran against web image
`sha256:c3641c97aed096adb9a2792c9a332273aaf5627c951ce5f412d0050808cd10fa`.
The canonical deployment receipt must remain passed with no failures and its
`envelope_head_sha` must equal the exact pushed handoff head; read those values
from `state/release/propertyquarry-local-deployment.v1.json` rather than copying
a self-referential commit into this file. The matrix used the principal-bound
release-probe credential through bounded stdin; neither that credential nor an
API token is present in the receipt.

Paid billing remains deliberately unavailable. The deployed API receives
neither the generic Sandbox PayPal credential material nor EA's generic
PayFunnels webhook secret; all dedicated Live credential inputs are empty.
PayPal and both PayFunnels paid plans report unconfigured, and both provider
safe handoffs report false. Every field in the exact-release
`propertyquarry.paid_billing_safe_handoff.v1` admission is empty, including the
provider, plan set, receipt/principal digests, release bindings, and verified
timestamp. Do not activate checkout until the same-principal checkout,
signed-idempotent webhook, entitlement grant, and cancellation canary has been
proved for the exact release and the external authority installs the bounded
admission.

Fresh physical-Android coverage is still an external device boundary. The
live audit at `2026-08-12T21:47Z` again found no native handoff sequence in the
preceding two hours. Production API telemetry contained zero requests to
`/mobile/runtime-contract`, `/sign-in/google`, `/google/callback`,
`/mobile/auth/bridge`, or `/mobile/auth/redeem`. The previously proven physical
flow is not a fresh pass for this exact web image; do not claim otherwise.

Encrypted off-host DR is also still externally blocked, but not for lack of
generic cloud capacity or tooling. The backup container is healthy on a
24-hour interval and its newest local artifact remains
`propertyquarry-20260812T092158Z.dump`, 42,344,555 bytes, with a verified local
SHA-256 sidecar. The host has writable pCloud, OneDrive, and Internxt remotes
and a verified pinned AWS CLI; the exact evidence and non-authoritative pCloud
canary are in the newer DR section above. It still has zero GPG public
recipients, no AWS principal, no locked-S3 configuration, no encrypted
artifact, immutable S3 version, provider read-back, disposable restore receipt,
or signed public-launch authority. Do not weaken this boundary or substitute a
plain sync mount for the required recovery proof.

## 2026-08-12 Austria closed test approved and first tester enrolled

Google has completed review of the Austria closed Alpha. The clean tester URL
now renders the PropertyQuarry invitation for `tibor.girschele@gmail.com` and
offered `Become a tester`; that already-authorized account was enrolled and the
read-back says `You are a tester.` The clean store URL now renders the complete
PropertyQuarry listing and offers installation for the account. Use these links
without adding a literal `/n` or `\\n` prefix:

https://play.google.com/apps/testing/com.myexternalbrain.propertyquarry

https://play.google.com/store/apps/details?id=com.myexternalbrain.propertyquarry

Play Console now reports Closed testing `Active`, `1 track`, the closed-release
prerequisite complete, and exactly `1 tester currently opted in`. Production is
still inactive and was not opened, promoted, edited, or submitted. Production
access still needs eleven additional distinct real testers to opt in and then
at least twelve testers to remain continuously enrolled for fourteen days.

EA delivered the approved tester and app links to the operator on Telegram as
message `5203`, using two clean URL buttons. The earlier instruction not to send
an unavailable invite is superseded: the invite is now live. Do not fabricate
the remaining testers or claim the fourteen-day clock is satisfied before Play
does.

## 2026-08-12 current LTD provider and publication truth

The apparent 1min.AI discrepancy is resolved. The deployed API process reports
zero locally configured 1min slots because it is only the enqueue/read surface;
the durable PostgreSQL queue is claimed by `propertyquarry-worker-live`, which
owns the provider credentials and performs the actual call. No credential needs
to be copied into the API process. A secret-safe worker probe at
`2026-08-12T20:14:50Z` reported `70` configured slots, `25` successful probes,
`45` depleted probes, `26` live-dispatchable slots, and `2` slots in the
composite `ready` state.

The existing real PropertyQuarry image generation was re-read from deployed
PostgreSQL without another provider call. Generation
`b77bb8dbc8cb4c94952fe2fdd1775fe1` remains `completed` after exactly one
attempt, using backend `1min` and model `gpt-image-1-mini`. Its receipt remains
`verified` with proof scope `provider_call`; the PNG is still materialized at
the principal-scoped first-party asset route with SHA-256
`635440acd09188c91b7b08ead3dc2a59fcd97281f7693565415ee1da8d3a8515`.
Publication remains exactly `not_published` with
`external_publication_verified=false`.

FlipLink remains an honest external-authority boundary, not a runtime defect.
The exact deployed API, worker, and scheduler were probed without exposing
secrets: none has a FlipLink login email, password, BrowserAct enablement, or
webhook secret. The API reports no verified account, tier, capability, or custom
domain and the deployed publication repository contains zero rows. The guarded
manual/BrowserAct contracts therefore remain disabled and the customer output
must continue to say `local_only` / `not_published`. Do not set the feature flag
alone or record a manual link as provider proof. A future live integration needs
the real account credentials, a verified redacted-upload capability and privacy
review, a principal-bound BrowserAct completion receipt, and the first external
publication read-back.

A separate Unmixr live-account probe at `2026-08-12T20:14:20Z` found six usable
accounts and aggregate capacity of `3,000,000` prebuilt credits, `500,302`
cloned-voice credits, and `22` cloned profiles. This is current EA/Chummer
provider-pool evidence only. Unmixr remains `catalog_only` for PropertyQuarry
and must not be represented as a PropertyQuarry customer integration.

The newer 2026-08-13 truth-separation section supersedes this inventory note.
`LTDs.md` and the runtime catalog now distinguish provider receipts, live
account/service evidence, contract availability, and customer integration.
No provider account or provider-side state was changed during either audit.

## 2026-08-12 fast-result thumbnail repair

A live authenticated comparison isolated the remaining intermittent thumbnail
gap. The full workbench route for Austrian run
`0d39c56749b04ec795302ad5e1ab6023` rendered all 10 thumbnails with nonzero
natural widths; all 10 image requests returned HTTP 200. The fast result route
`/app/shortlist/run/0d39c56749b04ec795302ad5e1ab6023`, however, rendered
zero image elements even though the same customer-safe candidate payloads
contained listing previews. Its JavaScript computed `previewHref` but then
selected only `dioramaHref`, so every candidate without a governed diorama was
silently reduced to a placeholder.

`app/property_ranked_run_fast.html` now uses the validated diorama first and
then the ordered listing/orientation preview chain on both server first paint
and JavaScript refresh. External images must be HTTPS, protocol-relative and
credential-bearing URLs are rejected, and image requests use
`referrerpolicy=no-referrer`. A failed source advances through at most six
deduplicated safe candidates; only an exhausted chain becomes the honest
`Property` placeholder. Property-detail links remain first-party, while the
separate listing link retains its existing external-link boundary.

The dedicated real-Chromium regression proves a failed primary recovering to a
valid fallback, a fully exhausted chain ending without a broken image, and a
provider-chrome primary being skipped before any request. The server first-
paint projection now uses the same allowlist and rejects the two exact
production patterns: Willhaben `/img/upselling/` and ImmoScout
`plus-insider-locked`. Genuine listing media remains in the bounded fallback
chain.

The final fix is commit `984f16a22e664b37348bb4b5fe4457dc34c4fdf6` on
`integration/property-origin-main-20260728`. The focused thumbnail/projection
set passes 27/27 and the complete workspace UI contract passes 885/885. Exact
Python compilation and `git diff --check` pass. The canonical deployment
receipt observed at `2026-08-12T19:58:59Z` is passed with no failures, runtime
manifest `e73c938906ef44f2c474cda22a8d17104c741bb7`, and web image
`sha256:8ba79aeff2352db3116a61f8579bb9339be3656a50f817a694884871db781adf`.

Authenticated production proof on the same Austrian run rendered 10 rows, 10
image elements, 10 images with nonzero natural widths, zero broken images, and
zero placeholders. No provider-chrome URL appeared in the rendered DOM or the
browser resource requests. A reversible DOM-only failure on the first listing
advanced to its 640 px first-party map preview, remained loaded, and did not
show the placeholder. Reloading restored the untouched server-backed view.
Treat the intermittent fast-result thumbnail incident as closed.

## 2026-08-12 Austria opportunity and verified LTD cover proof

The previously missing live opportunity proof now exists. Signed-in reviewer
principal `play-review-e514dab38606fbb72dbe` ran Austrian search
`0d39c56749b04ec795302ad5e1ab6023`; all 10 returned candidates received
persisted opportunity assessments with no failures. Opportunity
`property_opportunity:e7e5c646e205b13c872546bd25799cec3e4a981bbeda9dd3dd4eb60934c96f05`
on candidate `property-scout:1355793819` has useful brief artifact
`sum_f07010e1bf9443d68817e1316ce801c6`. The brief repair is in `61b6c3`, and
the customer-visible, receipt-backed cover lane is in commits `1fd3e2cc`,
`679118da`, `f7db9500`, `af5f636a`, and `ba53911e`.

One real 1min.AI worker call completed as generation
`b77bb8dbc8cb4c94952fe2fdd1775fe1`, using action `image_generate`, backend
`1min`, and model `gpt-image-1-mini`. The durable job remained at exactly one
attempt. Its principal-bound provider receipt is `verified` with proof scope
`provider_call`; publication remains honestly `not_published`, with
`external_publication_verified: false`. No listing address, listing title,
customer identifier, or opportunity identifier was sent in the provider
prompt.

The provider output is no longer exposed as an expiring presigned URL. The
worker now validates and privately materializes bounded PNG/JPEG/WebP bytes in
the API/worker shared artifact volume, while the browser receives only the
principal-scoped first-party asset route. Legacy completed generation rows are
lazily migrated with a principal-authorized compare-and-swap; the asset route
fails closed unless that sanitized receipt is durable. The real legacy row was
successfully upgraded without another provider call. Postgres now records:

- first-party asset
  `/app/api/property/opportunities/generations/b77bb8dbc8cb4c94952fe2fdd1775fe1/asset`;
- `image/png`, 1024×1024, 1,231,003 bytes;
- SHA-256
  `635440acd09188c91b7b08ead3dc2a59fcd97281f7693565415ee1da8d3a8515`;
- `asset_materialized: true`, matching receipt digest, `attempt_count: 1`;
- no external HTTPS URL, presigned query, provider-private response,
  `account_id`, or `slot_id` anywhere in the persisted job payload.

An authenticated live read returned HTTP 200, `image/png`,
`Cache-Control: private, no-store`, and an ETag containing that exact digest;
the downloaded 1,231,003 bytes independently hashed to the same value. The
compact customer UI is captured at
`state/qa/propertyquarry-ltd-cover-live-20260812.png`, SHA-256
`117b8542bb12820ecda27b23046e09da5dfcc79232833b17ea2c3ea915f0b18d`.
Its copy explicitly says `AI concept cover` and
`Synthetic mood illustration · not listing photography`.

The final focused cover/queue/opportunity/polling suite passes 55 tests, exact
compilation and `git diff --check` pass, and `vexp verify_done` reports no parse
errors or broken imports. The canonical deployment receipt is
`state/release/propertyquarry-local-deployment.v1.json`; after committing this
handoff, redeploy once so its envelope binds the exact pushed handoff head.

Do not mistake this proof for closure of the whole launch goal. Still required:
a fresh physical-Android pass after the latest web work, safe billing admission
before any activation, the Play closed-test/production-access prerequisites,
either real external FlipLink publication or continued `local_only` labelling,
and an off-host encrypted restore proof or precisely evidenced external-
authority blocker.

The fresh post-cover cross-browser live pass is complete. Receipt
`state/qa/propertyquarry-live-browser-all-20260812-current.json`, SHA-256
`b2ebbc6862292a92a52f91ecf531922a488f551480dc00f116c4a83dac016e24`,
records 48/48 Playwright samples and zero failures at
`2026-08-12T19:09:22.720710+00:00`: eight authenticated customer routes in
Chromium, Firefox, and WebKit at 390×844 and 430×932. It has no missing engine,
sample, or static fallback. All customer pages returned 200; the six billing
samples returned the expected fail-closed 503 and passed the explicit free-plan
compatibility contract. A separate authenticated retained-profile browser read
the private cover endpoint after that matrix and independently reproduced the
same status, media type, byte length, cache policy, ETag, and SHA-256. The
durable job still had `attempt_count: 1`; no second provider call occurred. The
HttpOnly authenticated session was not copied across engines, so the receipt
correctly treats the three-engine matrix as customer-page proof and the cover
read as a separate principal-scoped asset proof.

Fresh physical-Android coverage is not yet available for this exact web head.
At `2026-08-12 19:12 UTC`, this host had neither an `adb` executable nor a
configured Android SDK, and production telemetry since the final deployment
contained no `/mobile/runtime-contract`, `/sign-in/google`, `/google/callback`,
`/mobile/auth/bridge`, `/mobile/auth/redeem`, or `/app/search` request from a
new device run. This is an external device-availability boundary, not evidence
of a regression. Do not claim a fresh Android pass until the installed build 5
is opened on the physical device and live telemetry again shows, in order,
runtime contract 200, unauthenticated Search 303 if a new sign-in is needed,
Google start/callback 303, bridge 200, redeem 200, and authenticated Search 200.
No reinstall or Play-track change is needed for that check.

## 2026-08-12 Play reviewer access and declarations

The dedicated Google Play reviewer lane is now live and independently verified.
`/sign-in/play-review` accepts only the isolated reviewer principal, applies
rate limiting, creates a 24-hour workspace session, and opens authenticated
Search without Google, inbox/OTP access, payment, or a trial. Commits
`b42ada08`, `e2f76e1a`, and `43e8397d` make the form safe behind the production
tunnel: named origins still require the canonical same-origin check, missing
origins are rejected, cross-site named origins are rejected, and the literal
opaque browser origin `Origin: null` is accepted only at this exact
credential-gated reviewer endpoint. The final live browser proof reached
`https://propertyquarry.com/app/search` with the reviewer session.

The current reviewer digest is retained in the ignored runtime environment;
the plaintext credential was entered directly into Play Console and its two
validated mode-0600 temporary files were deleted afterward. Do not attempt to
recover or copy the plaintext from logs. Rotate the digest and update Play
Console together if reviewer access ever needs to change.

Google Play Console has now accepted the following changes into review:

- dedicated reviewer sign-in instructions and credential;
- target audience **18 and over** only;
- completed content-rating questionnaire (Brazil all ages, PEGI 3, USK all
  ages, generic 3+);
- completed Data Safety questionnaire with the public deletion URL
  `https://propertyquarry.com/data-deletion`;
- collected data limited to name, email, user IDs, diagnostics, app
  interactions, in-app search history, and other user-generated content;
- no device location, payment/purchase data, personal financial data,
  user-uploaded files/media, browsing history, installed-app inventory, crash
  logs, device IDs, advertising data, or data sale declared for this release;
  and
- no data declared as shared with third parties; contracted processor and
  user-directed transfers remain within the applicable Play exemptions.

The Austria closed Alpha release is no longer a draft. Existing app bundle
version code 5 / version name 1.1.3 was selected from Play's artifact library,
given German closed-test notes, reviewed as `Ready to release`, and saved to
Publishing overview. The selected track remains Alpha, its only country/region
is Austria, and its tester source is the six-address `PropertyQuarry internal`
email list. The list read-back includes all six known PropertyQuarry accounts,
including `elisabeth.girschele@gmail.com` and `tibor.girschele@gmail.com`.

Publishing overview was audited before submission. Its 13 changes consist of
the closed Alpha full rollout, Austria targeting, track resumption, the selected
tester list, German store listing, content rating, 18+ target audience, privacy
policy, ads declaration, Data Safety, Health apps declaration, House & home
category, and the already-entered reviewer/declaration details. It contains no
Production release, open-test rollout, billing change, or account-security
change. The bundle was sent to Google for review at approximately
`2026-08-12T20:08Z`; Play's quick checks completed, and Publishing overview now
states `Your changes are now in review. We may find additional issues when
reviewing your app.` Managed publishing remains off.

The closed track summary now reads `Active`, `Release 5 (1.1.3) in review`, and
`1 country/region`. Play exposes the closed-test web invite as
`https://play.google.com/apps/testing/com.myexternalbrain.propertyquarry` and
the Android store link as
`https://play.google.com/store/apps/details?id=com.myexternalbrain.propertyquarry`.
A signed-in read with `tibor.girschele@gmail.com`, which is on the selected
list, still returns `App not available` while the release is in review. Do not
send the invite to Telegram or claim the test clock has started until Google
approves it and that same URL shows the opt-in action.

The dashboard currently reports closed testing `Active · 1 track`, update
status `In review`, Production inactive, and exactly `0 testers currently
opted in`. Production access still requires at least 12 distinct opted-in
testers for at least 14 continuous days. The selected list has only six
addresses, so approval alone cannot satisfy the tester-count requirement;
additional real tester accounts and their actual opt-ins are external human
inputs. No Production release, promotion, application, or rollout was created.

The canonical deployment receipt is
`state/release/propertyquarry-local-deployment.v1.json`; use its
`envelope_head_sha` rather than copying a self-referential handoff commit hash
into this file. The final deployment after this handoff commit must retain
runtime manifest `e73c938906ef44f2c474cda22a8d17104c741bb7`, `passed: true`,
and an empty `failures` list while binding the envelope and web-image digest to
the exact pushed head.

## 2026-08-12 LTD integration audit

There are two distinct LTD concepts and they must not be conflated.
PropertyQuarry customer lifetime entitlements remain in the existing billing
and entitlement boundary; external lifetime-deal services are the operator
tool inventory in `LTDs.md`.

The external LTD runtime is already integrated through
`ea/app/services/ltd_runtime_catalog.py`,
`ea/app/services/ltd_runtime_skill_projection.py`, and the authenticated
`ea/app/api/routes/ltd_runtime.py` endpoints. It converts the inventory into
bounded executable actions, projects those actions into tool-and-artifact task
contracts, and exposes principal-bound proof scopes. Telegram can resolve and
execute eligible actions, and direct API execution rejects principal, handler,
action, or provider mismatches. The 1min generation lanes bind code, review,
image, and media receipts to the requesting principal. This is real local
integration, not proof that every owned provider is live.

The Austrian same-principal opportunity and 1min.AI generation proof is now
recorded in the newer section above. The PropertyQuarry search context still
does not silently call arbitrary LTD providers; generation remains an explicit
customer action and must stay receipt-gated. FlipLink publication is still
unavailable until live account, capability, privacy/export, and first-
publication receipts exist. Other Tier 2-4 inventory entries remain partial or
inventory-only exactly as recorded in `LTDs.md`. Do not label a catalog entry,
BrowserAct template, or local dry-run as an executed provider call.

## 2026-08-12 live thumbnail polish and Play listing completion

The later thumbnail-reliability follow-up closes two additional intermittent
paths. Transformed CDN image URLs such as signed resize paths and
`format=webp` query endpoints now survive the same customer-safe projection
that already accepted ordinary `.jpg` URLs; previously the listing extractor
kept these images and the workspace projection silently removed them.
Shortlist/area-preview images now participate in the existing bounded fallback,
refresh, no-referrer, and failure-placeholder controller instead of bypassing
it. A failed primary and all failed fallbacks therefore end in the visible
`Preview not available` state rather than a blank or broken image.

The final live audit also found two provider UI assets being projected as
property photos: Willhaben's `/img/upselling/` badge and ImmoScout24's
`plus-insider-locked` badge. The customer projection now excludes only those
known non-listing assets in both the JSON refresh projection and server-rendered
first paint. The two paths now share one ordered preview/fallback resolver. It
promotes the next valid listing image when one is available and otherwise uses
the honest unavailable placeholder; real provider photos, first-party map
previews, and transformed CDN images remain admitted.

Focused verification for this follow-up passes 34 projection/extractor/polling
tests including a real Chromium broken image, six thumbnail workspace
contracts, and the isolated Chromium lazy-atlas E2E. `git diff --check` is
clean. The broad `pytest -k thumbnail` collection
cannot be treated as one-process evidence because unrelated synchronous
Playwright fixtures collide with the suite's already-running asyncio loop; the
same affected Chromium tests pass in their isolated canonical invocations.

The deployed signed-in reviewer-principal search run
`0d39c56749b04ec795302ad5e1ab6023` returned 10 Austrian listings. Before the
provider-chrome filter, all 10 projected images loaded with nonzero natural
widths, which proved the fixed customer projection no longer drops valid remote
assets. A production-DOM-only fault injection then exhausted the first card's
fallback chain: the broken image became hidden, the thumbnail entered
`is-unavailable`, and `Medienvorschau noch nicht verfügbar.` became visible.
Reloading restored the untouched server-backed result. The browser session was
closed after verification. After the shared first-paint resolver was deployed,
a cache-busted reload of that same run rendered all 10 cards with nonzero image
widths, removed every Willhaben upsell and ImmoScout locked-content asset from
the DOM, and replaced them with first-party area previews or genuine gallery
photos. A second DOM-only failure on the first card advanced automatically to
its declared first-party map-preview fallback (640 px wide) without hiding the
image or showing the unavailable state. The original page was restored before
the final browser session was closed.

The intermittent-thumbnail repair is live and the shortlist now has a truthful
first-paint fallback. Commit `e75bf00812bfe21815c1b6da131b66a964590504`
renders a validated listing or orientation image when no governed spatial
diorama exists. Real dioramas remain labelled `Spatial diorama`; the fallback
is labelled `Area preview`; only a genuinely absent image shows `Preview not
available`. It no longer discards a valid first-party map preview and then says
`Diorama not ready`.

The canonical healthy deployment receipt at
`state/release/propertyquarry-local-deployment.v1.json` binds runtime manifest
`e73c938906ef44f2c474cda22a8d17104c741bb7`, envelope `e75bf00812bfe21815c1b6da131b66a964590504`,
and web image
`sha256:5ac3900cf5584f09b5ceb4b4cc2ae07a2593f94a0378d4fea9cc01eec7b8da27`,
observed at `2026-08-12T15:57:14Z`. Every deployed service is healthy and the
migration exited successfully. The subsequent source-only demo-fixture commit
`49ff2fdd` pins the synthetic 1020 Vienna card to explicit Leopoldstadt
coordinates; that exact payload was then written to the release-probe principal
through a signed in-container loopback request. It does not change customer
data or claim that the demo is a real listing.

The final live Play screenshots are tracked at:

- `mobile/store/graphics/phone-search-1080x1920.png`, SHA-256
  `18067d7189129ac3d739b7c6d031d20ad1a7425d6073fa56b27a78df1b4cb7ca`;
- `mobile/store/graphics/phone-shortlist-1080x1920.png`, SHA-256
  `52611db17f61355226499f5fdb7c897100ecd8397cb214f8c2d9ec9bc40a0711`.

Both were captured from live HTTP 200 authenticated release-probe pages in
Chromium at an exact 1080x1920 Play-compatible resolution. The private receipt
is `state/qa/propertyquarry-play-store-screenshots-current.json`, generated at
`2026-08-12T15:59:33.197181+00:00`. Visual inspection confirmed the final
shortlist basemap is Leopoldstadt/Prater, not the earlier incorrect geocoder
result. The earlier pre-cover cross-browser live receipt was
`state/qa/propertyquarry-live-browser-all-20260812-current.json`, SHA-256
`dc6b8d3b0dda12cf65e8c682b2461f3c8817e5cdfba2c849bf4bd350facd005f`,
with 48/48 real samples across Chromium, Firefox, and WebKit at 390x844 and
430x932, generated after the deployed fallback at
`2026-08-12T16:15:44.691224+00:00`. The expected paid-billing recovery samples
returned 503 and passed the explicit free-plan compatibility contract; all
other samples returned 200. It has now been superseded by the post-cover
receipt documented in the newer opportunity section above.

At the time of the thumbnail/listing slice, Google Play Console app
`4976153363318887490` reported **7 of 11** app-info tasks complete, up from 5
of 11 at the start of that slice. The default German
store listing is saved and marked `Ready to send for review` with the exact
tracked German copy, one 512x512 icon, one 1024x500 feature graphic, and both
1080x1920 phone screenshots. The icon and feature graphic are truthfully marked
as created or edited with AI; the factual live screenshots are not. The change
is waiting in Publishing overview. `Send for review` was deliberately not
pressed, and no production release or rollout was created.

Those four then-incomplete Play app-info tasks were **Sign-in details**,
**Content rating**, **Target audience**, and **Data safety**. They are now saved
as described in the newer section above. Play still requires its
closed-testing and production-access process; Production remains inactive.
Preserve the existing internal release and tester configuration unless
explicitly asked to change them.

Final focused verification for this slice passed 123 Python
thumbnail/workbench/fixture/screenshot/accessibility tests, all 11 mobile Node
contracts, exact compilation, and `git diff --check`. `vexp verify_done`
reported no parse errors or broken imports; its pending-dependent list is the
known stale global-EA index projection rather than the PropertyQuarry working
tree. Continue to preserve the
existing honest boundaries: paid billing lacks an exact-release provider
canary admission, external FlipLink publication remains unavailable, physical
Android coverage has not been rerun for these web-only thumbnail changes, and
encrypted immutable off-host DR still lacks external AWS/key/restore authority.

## 2026-08-12 encrypted off-host DR boundary

The source commit containing this handoff adds the concrete, fail-closed AWS S3
backup verifier at `scripts/propertyquarry_s3_backup_verify.py` and strengthens
restore validation in `scripts/propertyquarry_postgres_dr.py`. The verifier
accepts only an exact encrypted artifact descriptor, attests the tracked pinned
AWS CLI, uploads to the official regional S3 endpoint with SHA-256, `AES256`,
and Object Lock `COMPLIANCE`, then performs provider-native head and exact-version
read-back. It independently streams the downloaded descriptor through SHA-256
and requires both AWS responses to preserve the version, ETag, length, checksum,
digest metadata, `AES256`, COMPLIANCE mode, and requested retention. Backup
receipts now require active COMPLIANCE retention extending at least seven days;
the concrete helper only accepts configured retention of 30-3,650 days.

Focused verification passes `105/105` DR and locked-S3 tests, exact compilation,
and `git diff --check`. The tests include plaintext refusal, noncanonical key
prefix refusal, streaming read-back checksum enforcement, immutable version
binding, weakened head/get Object Lock refusal, changed encryption refusal,
distinct provider-request identities, and restore-side retention/encryption
refusal. The DR runbook now documents the exact helper, release-tree
installation, bucket prerequisites, required configuration, and critical-data
contract/evidence version 4.

This does **not** satisfy the live DR launch prerequisite. The read-only runtime
audit at `2026-08-12T15:07Z` found:

- `propertyquarry-backup-live` running healthy with only the legacy local
  plaintext daemon and no encryption, S3, Object Lock, region, or AWS credential
  configuration;
- the newest observed local dump at
  `/backups/propertyquarry-20260812T092158Z.dump`, 42,344,555 bytes;
- the tracked AWS CLI pin resolving to the exact host AWS CLI 2.35.16, but STS
  failing with `NoCredentials`;
- zero GPG public keys and no configured backup encryption recipient; and
- no host `pg_dump`, `pg_restore`, or `psql`, hence no authorized disposable
  restore-drill environment.

No encrypted artifact, immutable S3 version, provider receipt, disposable
restore, RPO/RTO receipt, or DR release-gate receipt was fabricated or claimed.
External authority must provide an approved AWS identity and bucket with
Versioning/Object Lock, the recovery public key and recipient fingerprint, and
an isolated disposable PostgreSQL restore target/toolchain. Then install the
two DR scripts plus tracked pin manifest together in a trusted release tree,
run a fresh encrypted backup through the locked-S3 helper, retrieve that exact
version, complete the disposable restore drill, and run the exact-release gate.

## 2026-08-12 fail-closed paid-billing admission

Commit `046c49a4` is published on
`integration/property-origin-main-20260728`. A secret-safe inspection of the
deployed API found PayPal enabled with a client ID and secret, while PayFunnels
had only a webhook secret and neither an API key nor plan checkout URLs. Before
this commit, those PayPal settings alone made the generic authenticated paid
checkout select PayPal even though no same-principal/webhook/entitlement canary
had been externally proven.

Provider credentials are now necessary but insufficient. PayPal and PayFunnels
customer checkout remain unavailable unless the API also receives a fresh
`propertyquarry.paid_billing_safe_handoff.v1` admission bound to:

- the exact `PROPERTYQUARRY_RELEASE_COMMIT_SHA` and
  `PROPERTYQUARRY_RELEASE_IMAGE_DIGEST`;
- one exact provider (`paypal` or `payfunnels`) and both paid plans
  (`agent,plus`);
- non-secret SHA-256 identities for the external receipt and its canary
  principal; and
- a `VERIFIED_AT` timestamp no older than 24 hours (with at most five minutes
  of future clock skew).

Compose forwards only those non-secret admission fields to the API process.
The external release authority must set them only after proving checkout keeps
the authenticated principal without a second login, webhook processing is
signed and idempotent, and entitlement grant plus cancellation both work. The
environment admission is a kill-switch boundary, not a substitute for the
external evidence or the signed public-launch authority receipt. A provider or
release mismatch, a missing digest, an incomplete plan set, or stale evidence
fails closed. PayPal capture also rechecks admission before contacting the
provider; inbound signed reconciliation remains available for provider events
already in flight.

Verification passed `26/26` focused checkout and lifetime-entitlement tests,
exact Python compilation, Compose parse without interpolation, and
`git diff --check`. `vexp verify_done` again returned its known stale global-EA
index projection rather than the PropertyQuarry working tree; it reported no
broken imports or parse errors, and the PropertyQuarry tests above are the
authoritative result.

This fix is included in the later live descendant deployment at commit
`31dd6f789535979b3798cf2fb1a4428967c05b97`. Paid checkout nevertheless remains
fail-closed because no exact-release external canary admission was installed.
Do not populate the handoff fields until checkout, principal preservation,
signed idempotent webhooks, entitlement grant, and cancellation have actually
passed for the exact deployed commit and image.

## 2026-08-12 principal-bound LTD execution truth

Commit `4ef0fc5e` is published on
`integration/property-origin-main-20260728`. Telegram no longer describes an
LTD catalog entry or action route as live execution. Catalog replies now state
that inventory does not prove credentials, provider health, or a live call.
The bounded 1min media path says `Executed` only when the exact Telegram
principal, handler, invocation contract, provider/backend, feature, model,
target, and output asset all agree; an incomplete or foreign receipt is
reported as unproven.

The authenticated LTD action API now applies the same fail-closed boundary.
It rejects a result not bound to the requesting principal, requires exact
handler and action identity, and requires provider identity for direct provider
tools. Successful responses contain a small customer-safe proof projection
whose `proof_scope` distinguishes `provider_call`, `browser_session_call`, and
`principal_bound_tool_invocation`. Raw provider account labels, key-slot names,
binding/workflow/task identifiers, requested runner URLs, and upstream raw
responses are removed from the response. Internal BrowserAct target references
are reduced to the action identity. All 1min code, review, image, and media
adapter receipts now contain the exact request principal; generated summary
text no longer embeds the internal provider-account label.

Verification on the committed bytes passed:

- full authenticated LTD runtime API: `7 passed`;
- every Telegram local-assistant contract: `14 passed`;
- every 1min tool-execution contract: `25 passed`;
- exact module compilation and `git diff --check`: passed.

This source commit is included in the later live descendant deployment at
`31dd6f789535979b3798cf2fb1a4428967c05b97`; the old Google device-challenge
handoff is no longer active. Deployment does not turn an inventory entry into
provider proof. Rerun principal-bound Austrian search, opportunity, generation,
and LTD calls whenever external credentials or provider state change, and keep
every unproven action labeled honestly.

## 2026-08-12 exact public-launch authority handoff

The source commit containing this handoff makes the remaining public-launch
blocker directly actionable without weakening external authority. The launch
room now emits an `authority_handoff` object with the fixed signed-receipt
contract and path, fixed trust-store environment variable and path, exact
envelope/runtime/image bindings, receipt constraints, and the three evidence
requirements with their digest contracts. It distinguishes
well-formed candidate values from an exact deployment: `bindings_complete`
cannot become true until the local deployment receipt proves this exact
envelope. Local tests or locally created JSON can never substitute for the
external signature.

Each requirement now names what must actually be proven. Google Play requires
production access, an Austria-active production release, completed app/store
content, and installation without an internal-tester invitation. Billing
requires configured paid-plan checkout, same-principal handoff without a
second login, signed idempotent webhooks, and entitlement grant/cancellation.
Those two receipts remain explicitly external and have no repository verifier.
DR points to the existing
`propertyquarry.postgres_dr_receipt.v3:release_gate` contract and
`scripts/propertyquarry_postgres_dr.py release-gate`, which already verifies an
exact-release encrypted backup, immutable off-host object, provider-attested
retrieval, disposable restore, RPO/RTO, schema, and critical-data survival.

The handoff also observes—but does not trust—whether the fixed receipt, trust
store, and trust-store environment binding are present. The read-only host
audit at `2026-08-12T14:20Z` found both fixed files absent:

- `/run/propertyquarry/release-control/propertyquarry-public-launch-authority.v2.json`;
- `/etc/propertyquarry/release-control/global-governance-trust-store.v1.json`.

The current deployment receipt remains stale for the source envelope, so the
authority handoff is correctly `blocked_local_runtime_precondition` even though
the runtime commit and recorded image digest are syntactically well formed.
The stale launch-room instruction to configure a verifier was removed; the
verifier already exists. Once the explicit browser challenge is complete, the
correct order is: deploy the exact candidate, collect passing Google Play,
paid-billing handoff, and encrypted off-host restore evidence, then have the
external governance authority install its signed canonical receipt and pinned
trust store at the fixed paths.

Verification on the candidate bytes passed `18/18` launch-room and signed
public-authority tests, exact Python compilation, and `git diff --check`. No
deployment, browser command, app restart, Play change, billing activation, or
DR claim was made.

## 2026-08-12 resilient result thumbnails

Commit `31dd6f789535979b3798cf2fb1a4428967c05b97` is pushed and deployed through
the canonical PropertyQuarry authority. It repairs intermittent missing result
thumbnails without weakening the existing image-URL safety boundary. Result
cards previously projected only one remote listing image; a transient CDN,
hotlink, or Android WebView load failure immediately hid it even when another
safe listing image or a first-party preview was available.

Candidate payloads now retain up to four bounded, deduplicated, validated
fallbacks and prefer a first-party preview when one exists. The browser advances
through that safe chain, sends no referrer to hotlink-sensitive image hosts,
restores the normal thumbnail state after any successful retry, and exposes the
existing `Media preview not available` placeholder only after the bounded chain
is exhausted. Remote fallbacks remain HTTPS-only; same-origin HTTP is accepted
only for local development and test hosts. Live polling cards carry the same
fallback and referrer-policy contract as the initial server-rendered cards.

Verification on the deployed bytes passed:

- focused payload/backend contracts: `17 passed`;
- the broader related suite: `36 passed`;
- the real retry/exhaustion journey in Chromium, Firefox, and WebKit:
  `3 passed`;
- exact changed-module compilation and `git diff --check`: passed.

The live receipt binds envelope
`31dd6f789535979b3798cf2fb1a4428967c05b97`, runtime-manifest SHA
`e73c938906ef44f2c474cda22a8d17104c741bb7`, and web image
`sha256:5d3bb782bd5de73da8e110d17e13e4d16a8505d7bf8cbe4361612857544970f5`,
observed at `2026-08-12T14:59:58Z`. Public BrowserAct verification returned
HTTP 200 from readiness and confirmed the deployed candidate/failure-ledger
asset contract.

The canonical `property-release-gates` target still fails closed before running
its broader suite because `PROPERTYQUARRY_DR_BACKUP_RECEIPT` and
`PROPERTYQUARRY_DR_RESTORE_RECEIPT` are unset. This is the already documented
off-host disaster-recovery launch boundary, not a thumbnail regression; do not
bypass it or report the full release gate as passed.

## 2026-08-12 live opportunity, browser, and launch-boundary closure

Commit `c8def9d7` is published on
`integration/property-origin-main-20260728` and deployed through the canonical
local authority. The healthy deployment binds runtime
`e73c938906ef44f2c474cda22a8d17104c741bb7` to envelope
`c8def9d782d084f64c5205985542d6e260aeeae3`. The deployment receipt is
`state/release/propertyquarry-local-deployment.v1.json`.

The signed Austrian search run `c08b209d659047dc9d287ff372c43fd9` now has
useful durable opportunity evidence instead of a generic 50/100 placeholder.
For `property-scout:849451262`, the deployed service refreshed and then read
back the same PostgreSQL assessment in a fresh process:

```text
principal: propertyquarry-live-search-proof
assessment: property_opportunity:a1ec5c2566941a17442f7088032beeedc4aa9e0a3a170cabd514b0cc67aa2336
score: 74/100
confidence: 0.68
recommendation: shortlist
match reasons: 5
unknowns to verify: 6
blocking constraints: 0
repository: PostgresPreferenceProfileRepository
```

The reasons are grounded in the active search brief: EUR 2,680 is inside the
EUR 3,500 ceiling, 89 m² clears the 45 m² minimum, 1020 Wien is selected,
terrace/balcony evidence matches the request, and a floor plan is available.
Lift access and five exact-location amenities remain explicitly unverified.
The generated private artifact
`sum_0b38d6ed33eb4dfabfc475f8d6853220` was also read back in a fresh process.
It is stored as `PropertyQuarry / local_opportunity_brief /
durable_preference_assessment`, contains the score, recommendation, fit reasons,
and next checks, and has no doubled punctuation.

Search execution now passes active search preferences into each assessment,
compact PostgreSQL run storage retains the opportunity projection and counts,
manual generation refreshes stale rows, and candidates in the research bucket
remain resolvable. Verification on the committed bytes passed:

- focused opportunity, preference, artifact, API, and UI contracts: `25 passed`;
- broader storage, fact, queue, retention, and tour contracts: `156 passed, 1 skipped`;
- upstream and tool-execution regressions: `278 passed`;
- exact changed-module compilation and `git diff --check`: passed.

The same run retains a real principal-bound 1minAI call for
`property-scout:1217728088`: status `succeeded`, manager-routed, model
`deepseek-chat`, backend `1min`, slot `fallback_35`, account
`ONEMIN_AI_API_KEY_FALLBACK_35`, evaluated at
`2026-08-12T10:59:07.873225+00:00`. The worker-managed credential pool keeps
the secret outside the provider-binding row. Its current health is degraded
because 69 of 70 declared slots are unavailable, but the selected slot is
ready and the stored provider receipt proves a real call. Do not reinterpret
the generic environment-only live-ops probe's zero-slot operator projection as
absence of this principal-bound binding.

External FlipLink publication remains honestly unavailable. The opportunity
response reports `FlipLink.me`, action `publish_property_flipbook`,
`executable=false`, and `status=not_configured`; no external flipbook was
created. A separate live Unmixr account probe found seven usable accounts, but
Unmixr remains `catalog_only` for this PropertyQuarry principal and is not a
customer integration.

Fresh signed cross-browser live E2E is complete for the free customer path.
After installing the matching pinned Firefox and WebKit Playwright runtimes,
the browser-all harness passed `48/48` real samples: eight canonical routes,
all 18 registered customer-visible surface keys, Chromium/Firefox/WebKit, two
mobile viewports (`390x844`, `430x932`), and the concrete persisted research
detail. There were zero failed routes, missing engines, missing samples, or
static fallbacks. The private mode-`0600` receipt is
`state/qa/propertyquarry-live-browser-all-20260812.json`, SHA-256
`fbb5878fefda213962911c487e34efa0cbbc6e5788a9c9c5aa5412d04146cdef`, generated
at `2026-08-12T11:36:10.737681+00:00`. A separate rendered BrowserAct smoke
also proved the public sign-in surface and the demo decision conversation live.

The remaining launch boundaries are current and explicit:

- **Billing:** the free path is compatible and fails closed, but the paid
  persona is not launch-ready. `/app/billing` returns the intentional 503
  recovery surface; `billing.propertyquarry.com` redirects to a separate login,
  and the Plus/Agent PayFunnels checkout URLs are unset. Do not activate or
  advertise paid checkout until a no-second-login handoff passes.
- **Google Play:** a read-only Console audit found the app remains a Draft,
  Production is inactive, and app setup is `0 of 11` completed. The pending
  declarations are privacy policy, sign-in details, ads, content rating,
  target audience, data safety, government-app status, financial features,
  health, category/contact details, and Store Listing. Production access then
  requires a closed release with at least 12 opted-in testers for at least 14
  days; the closed track currently has 0 opted-in testers. Internal release 5
  and its six-email tester list were not changed.
- **Disaster recovery:** the live backup service has local rotation and
  retention only. It has no encrypted off-host provider/bucket configuration,
  and there is no current v2 backup, off-host retrieval, restore-drill, or
  release-gate receipt bound to this release. Do not claim off-host DR.

Physical Android Google sign-in remains closed by the successful device receipt
below. The work still requiring external credentials, policy answers, tester
participation/time, or infrastructure authority is not an application-code
failure and must remain fail-closed.

## 2026-08-12 opportunity-generation truth correction

The prior opportunity slice correctly reported that external FlipLink
publication was unavailable, but it still attributed the locally composed
private brief to `FlipLink.me`. That was too strong: the FlipLink catalog action
is non-executable, and no external provider is called when the brief is built.

The source candidate containing this handoff corrects the contract end to end:

- the artifact records `generation_provider=PropertyQuarry`,
  `generation_mode=local_opportunity_brief`, and
  `generation_basis=durable_preference_assessment`;
- the API separates local `generation` from the future `publication` contract;
- the current FlipLink publication state is `not_configured`, not generated or
  published;
- the browser displays `Private brief · PropertyQuarry`; and
- the brief now carries the match evidence, preference-fit score,
  recommendation, first risks to watch, and next unknowns to verify.

Focused API, source, summary, search-opportunity, and packet regressions pass
`15/15`. The real Chromium opportunity journey passes `1/1` and proves the
truthful label plus decision-evidence copy in the rendered result card.

This remains source evidence until the candidate is published, rebound,
deployed, and exercised against the live PostgreSQL database. At the start of
this correction the live database contained zero
`property_opportunity:%` assessments, so no live-integration claim is allowed
until a new signed-in Austrian search persists an assessment and the generation
route persists its corresponding private artifact.

## 2026-08-12 durable opportunities and LTD-generated private briefs

Source commit `cc8e95f6` completes the missing opportunity and LTD-generation
slice without changing the live Android bundle or Play track. Every processed
property search now evaluates discovered candidates against the selected
person's preference profile and projects the result as a customer-safe
opportunity. The durable assessment identity is deterministically bound to the
principal-scoped search run, domain, and candidate. Repeated status polling
upserts that exact row in both memory and PostgreSQL instead of creating
duplicate assessments; a new run receives a distinct assessment identity.

Opportunity judgment is additive. It no longer overwrites the search engine's
established ranking recommendation or reasons. Result cards expose the
opportunity recommendation, the first tradeoff to watch, and the first unknown
to verify. The full results surface offers `Create brief`; the compact shortlist
keeps its minimal `Review`, `Open property`, and remove controls.

`POST /app/api/property/opportunities/{candidate_ref}/generate` resolves only an
exact candidate from the caller's principal-scoped run, reuses the durable
assessment when present, and fails closed if persistence or the LTD lane is
unavailable. It creates a private contextual summary artifact through the
runtime-managed `FlipLink.me` LTD profile. The response truthfully reports
external publication as `not_available` while the catalog action is
non-executable; it never claims a public flipbook or leaks private assessment
fields into the browser.

Final evidence on the committed bytes:

- opportunity/preference/API/UI contracts: `20 passed`;
- expanded property-search, Teable, summary, packet, and LTD regression slice:
  `553 passed, 4 skipped`;
- remaining indexed facade dependents, including dossier, provider, spatial,
  and Telegram workflows: `291 passed, 1 skipped`;
- focused real-browser opportunity generation plus selection: `2 passed`;
- complete isolated PropertyQuarry browser gate: `130 passed, 1 skipped` in
  `878.40s`;
- Python compilation and `git diff --check`: passed.

One earlier full-browser attempt passed all `121` cases it reached, then the
shared pytest temp root was deleted externally and the final nine cases could
not set up. Those nine passed independently under an isolated base, and the
complete isolated rerun then passed with the count above. This was a harness
filesystem failure, not an application failure.

At the moment this section was written, the source candidate was committed but
not yet bound into a refreshed release envelope or deployed. Preserve that
truth boundary: publish and verify the candidate first, materialize its exact
release evidence second, and deploy only through the reconciled independent
release authority. Do not send replacement tester/app links through Telegram
until the exact live provenance and tester eligibility are reverified.

## 2026-08-11 search-flow polish and lifetime entitlement boundary

Commit `78deb91a` is published on
`integration/property-origin-main-20260728`. The Search setup now uses `Next`
for every intermediate step and replaces that control with the real localized
`Launch search` action in the same slot on the final Providers step. The old
top-bar duplicate is gone; the hydration guard moved with the real launch
button. German and Spanish mobile labels wrap cleanly without overflow.

The complete browser run recorded `121` passes before its `13` stale
pre-change interactions were identified. Those `13` affected cases were
updated and rerun successfully; one intentional skip remains. The focused
hydration and lifetime-entitlement suite passed `23/23`, rendered JavaScript
syntax passed, `git diff --check` passed, and the repository completion check
reported no parse errors or broken imports.

The lifetime Agent operator path now supports the deployed database generation
where `propertyquarry_google_identity_accounts` is not present. A live dry-run
for Elisabeth still returned `target_account_not_found`; no database mutation
occurred. Do not synthesize a principal or treat a tester-list email as account
identity. Elisabeth must first sign in to PropertyQuarry with the exact Google
account; only then rerun the dry-run and apply the idempotent lifetime Agent
grant with its private rollback snapshot.

The code is pushed but is not deployed to the live web runtime. The production
runbook requires the independently installed release-control executable at
`/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller`;
it is absent on this host. Do not bypass that boundary with the checkout-local
Compose helper. Preserve Play internal release 5 unchanged.

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

## 2026-08-12 continuation: thumbnails, LTD value, and launch truth

The intermittent result-thumbnail gap is fixed and already deployed. Initial
cards and live-polled cards now share the same bounded, no-referrer fallback
chain with up to four safe images. Commit `328deb16` was deployed through the
authoritative local-Docker lane; the live asset contains the fallback payload,
fallback index, and live no-referrer behavior. Deployment receipt:

```text
state/release/propertyquarry-local-deployment.v1.json
release image sha256:ddbf0fe802a74e2e463525c8f9ae77290e0c9323ba805e31968f58c9c11e664f
```

Launch-room truth was split correctly in `a7256c32`: local runtime readiness no
longer implies public-launch authority. Follow-up commit `8e3937d7` closes a
forgery gap: an unsigned or checkout-local JSON file can never grant public
launch. Commit `0bf55763` implements the missing verifier by reusing the
existing external global-governance Ed25519 trust boundary. Launch remains
fail-closed until release control provisions both of these fixed, root-owned
paths outside the checkout:

```text
/etc/propertyquarry/release-control/global-governance-trust-store.v1.json
/run/propertyquarry/release-control/propertyquarry-public-launch-authority.v2.json
```

The v2 receipt must be canonical strict JSON, short-lived, and signed by a key
authorized for the `global_market_envelope` gate. Its signature binds the
canonical repository, exact envelope HEAD, manifest runtime commit, deployed
image digest, nonce, and exact evidence digests for Play public launch, safe
paid-billing handoff, and encrypted off-host disaster recovery. Missing,
unsigned, stale, forged, checkout-local, caller-selected, partially populated,
or differently bound receipts remain blocked. The launch room only projects
non-secret verification identities and evidence references.

Commit `a88ce53c` closes the customer-visible part of the proven LTD lane.
PropertyQuarry already executed and persisted 1minAI `property.evaluate` calls,
but its sanitized public projection was not carried to result cards. Initial
page loads and live polling now receive the same exact, manager-routed,
input-digest-bound assessment and render a concise `1minAI evidence review`.
The customer payload deliberately omits provider account names and key slots.
A detached or incomplete receipt produces no AI claim.

Verification through the signed launch-authority commit:

```text
60 passed
rendered property-workbench JavaScript: node --check passed
Python compile and git diff checks: passed
```

Existing deployed proof for the PropertyQuarry proof principal remains:

```text
principal: propertyquarry-live-search-proof
run: c08b209d659047dc9d287ff372c43fd9
candidate: property-scout:1217728088
provider receipt: succeeded|true|1minAI|1min|deepseek-chat|fallback_35|ONEMIN_AI_API_KEY_FALLBACK_35|2026-08-12T10:59:07.873225+00:00|0|0
```

This proves a real persisted 1min call receipt. It does not substitute for a
fresh signed-in Tibor search. Unmixr live inventory was healthy (7 usable, 0
depleted, 0 unavailable), but it has no PropertyQuarry principal-bound binding
or customer-visible call receipt; keep it labeled inventory-only, not
integrated.

### 2026-08-12 FlipLink live-truth closure

Commit `ba99af68` removes the remaining static-catalog substitution from
opportunity-brief generation. A private brief no longer depends on the
FlipLink LTD catalog or reports catalog executability as publication
readiness. The API and persisted artifact now say `local_only`,
`private_artifact`, `not_published`, and
`external_publication_verified=false`; no external provider or receipt is
invented.

The deployed API was checked read-only at `2026-08-12T14:02:22Z`. It has no
FlipLink login email, login password, BrowserAct enablement, webhook secret, or
custom-domain configuration. Deployed PostgreSQL has zero packet-publication
rows, zero `published` rows, and zero external-URL rows. Therefore there is no
live FlipLink integration to claim.

The sharing dashboard now labels repository capacity as local packet storage,
hides provider plan/tier/domain claims, distinguishes a manually recorded
external link from a provider publication, and suppresses Publish assist while
the external account is unconfigured. Enabling only the feature flag is no
longer sufficient: the BrowserAct request boundary also requires configured
FlipLink credentials. Configuration still does not become a verified account
or provider-call claim without a future principal-bound probe and receipt.

Focused opportunity, summary, packet, dashboard-rendering, and FlipLink truth
regressions pass `16/16`; Python compilation and `git diff --check` pass. This
commit is published source only until the protected Google handoff finishes
and the current envelope can be deployed safely.

### Active Google human handoff

BrowserAct session `pq-live-thumbnail-20260812-root` on browser
`111111582245966456` is paused under human remote-assist lockdown at Google's
device challenge. The requested number is `61`. Do not issue any BrowserAct
session command until the user explicitly replies that the challenge is done.

Remote-assist link (it may need regeneration if expired):

https://www.browseract.com/remote-cli/870cc3b32e5d4e898384ae96239c3d14

After the user explicitly confirms completion: resume that exact session,
finish the signed-in Austria search, prove a fresh opportunity assessment and
summary artifact in deployed PostgreSQL, then deploy the current envelope so
the launch-room and 1min customer projection commits are in the live image.
Do not restart the app stack while the Google challenge may still be active.
