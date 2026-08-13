# PropertyQuarry local repository audit

- **Observed at**: 2026-08-13
- **Checkout**: `/docker/property`
- **Branch**: `integration/property-origin-main-20260728`
- **Upstream**: `origin/integration/property-origin-main-20260728`
- **HEAD**: `2c9df6c41f08d83570c01424f57591a27192b588` (`docs(release): record final property audit evidence`)
- **Scope**: independent local audit of the checkout, git tree, host operator plane, and running runtime. Extended the same day with implementation, design, product-function, and LTD-integration gaps.
- **Not in scope**: GitHub PR review, public-launch authority, secret values, or a claim that this file makes production ready

This document is operator evidence. It is not
`/run/propertyquarry/release-control/propertyquarry-public-launch-authority.v2.json`
and cannot satisfy a launch-room blocker.

## Verdict

The product runtime is locally healthy and fail-closed on the remaining
launch gates. The operator host and source tree are not in the clean
state the latest handoff claims.

- PropertyQuarry API, worker, and scheduler were healthy on web image
  `sha256:b98bfbb246fc51812fc60d1bc1121c86f453339886b1a993b0b56f3518894cff`
  and render image
  `sha256:98dfcedb74ab97862a88cc985ee7a795696cbc6f080793c37883054aec33b79e`.
- Launch room
  `state/qa/propertyquarry-launch-room-20260813-final.json` reports
  `local_runtime_ready=true` and `production_launch_ready=false`.
- Public launch remains blocked by the same four external authorities.
- The working tree was clean at audit start and then changed during the
  authorized repair pass. The LTD gate correction and security repair are
  reviewed below and must be published as a new candidate before the tree is
  treated as clean again.
- Host disk was 94% full, with 46 GiB free. This checkout alone is 31 GiB.
- No live API keys, PEM private keys, or GitHub PATs were found in
  tracked source. The compile-time private-showcase identities and exposed
  dump mode were repaired during this pass. Host-local dumps, a local GnuPG
  keyring, and a BrowserAct owner token visible in process argv still require
  disciplined operator handling.

Do not treat HEAD as a clean signed release envelope. Do not mark the
long-running launch goal complete.

## Snapshot

| Item | Value |
| --- | --- |
| Tracked files | 2,536 |
| Tracked Python | 1,063,211 lines |
| Tracked tests | 419,792 lines |
| Tracked markdown | about 2.4 MiB |
| Git object store | 296 MiB |
| Working tree | 31 GiB (`state/` 25 GiB, `_completion/` 2.1 GiB, `.propertyquarry_release_tools/` 2.8 GiB) |
| Ignored files | 196,705 |
| `origin/main` | ancestor of HEAD; HEAD is 306 commits ahead |
| Role policy | `ArchonMegalon/propertyquarry` canonical; `ArchonMegalon/property` legacy verifier |
| Isolation gate | `ok` |
| Role gate with `--require-clean-worktree` | fail: `worktree_not_clean` |
| Role gate without clean-tree requirement | `ok` |
| Deployed runtime SHA | `0a44ea202695163cf00dc8807c69d55dd0a561fc` |
| Envelope SHA | `4fb376e0e8113fe7383e765a6190b7b6aa902bb4` |
| HEAD versus deploy | four later docs/chore commits, then dirty LTD edits |
| Original issue counts | 4 bugs, 10 suggestions, 3 nits |
| Extension issue counts | 2 design bugs, 6 design suggestions, 1 design nit; 1 implementation bug, 6 implementation suggestions, 1 implementation nit; 10 product/LTD opportunity suggestions, 1 opportunity nit |
| Repaired in this pass | LTD evidence semantics, compile-time showcase identities, dump mode |

## Issues

### Issue 1 -- Severity: bug

- File: `scripts/verify_ltd_flagship_subset.py`
- Also: `tests/test_ltd_flagship_subset_gate.py`
- Description: The latest handoff commit described the preceding clean signed
  tree. During the authorized repair pass these two files were changed to split
  `live_evidence_verified` from
  `propertyquarry_customer_integration_verified` and to change
  `live_verified_total` from 5 to 1. This is an intentional correction: live
  provider/account evidence is no longer inflated into a PropertyQuarry
  customer integration. The focused critical and flagship verifier tests pass.
- Suggestion: Publish the semantic split explicitly and keep the legacy field
  documented as customer-integration evidence.
- Status: repaired; pending publication in the commit containing this audit

### Issue 2 -- Severity: bug

- File: `ea/app/product/service.py:13707`
- Description: Private-showcase access was hardcoded to two personal email
  identities. `_property_private_showcase_allowed_emails()` now reads only
  `PROPERTYQUARRY_PRIVATE_SHOWCASE_ALLOWED_EMAILS`; an empty value disables
  the feature, and normalized configured identities are the only admitted
  principals.
- Suggestion: Keep the allowlist in the ignored deployment environment and
  rotate it there without source changes.
- Status: repaired with focused default-deny and configured-identity tests

### Issue 3 -- Severity: bug

- File: `state/backups/propertyquarry-postgres/propertyquarry-pre-bounded-cleanup-b6fafd19966c.dump`
- Description: The 289,031,550-byte PostgreSQL dump was mode `0644` even
  though the larger maintenance dump was correctly restricted.
- Suggestion: Keep backup writers defaulting to owner-only access and retain
  the permission check in operational audits.
- Status: repaired; the dump is now `0600` and owned by `tibor:tibor`

### Issue 4 -- Severity: bug

- File: host process table (`browser_act_cli.session`)
- Description: A live BrowserAct session was running with
  `--browseract-session-server-owner-token` visible in `ps` output. That
  is a reusable owner credential in a world-readable process listing.
  The token value is not recorded here.
- Suggestion: Pass the owner token via a mode-`0600` file or an
  environment that is not copied into argv. Restart and rotate the
  current session token.
- Status: open

### Issue 5 -- Severity: suggestion

- File: host filesystem `/` and `/docker/property`
- Description: Root filesystem was 651/697 GiB used (94%, 46 GiB free).
  This checkout is 31 GiB. Docker reported 78 images / 44 GiB, 133
  containers / 93 running, 161 volumes / 34 GiB, and 8.8 GiB build
  cache. Local `state/` still holds Wine trees, vendor
  installers/extracts, Go toolchains, reconstruction stages, and
  multiple database dumps. 66 git worktrees were attached, most marked
  prunable. This matches the earlier host-capacity incident and will
  fail deploys and backups first.
- Suggestion: Prune stale worktrees and unused candidate stacks,
  especially `ea-core-candidate-20260713t231645z-64e03e1c-*`. Do not
  delete the current PropertyQuarry web or render images or the newest
  backup dump. Bound `state/` retention.
- Status: open

### Issue 6 -- Severity: suggestion

- File: `state/qa/propertyquarry-launch-room-20260813-final.json`
- Description: Public launch is correctly not ready. Remaining blockers
  are `external_public_launch_authority_receipt_missing`,
  `google_play_public_launch_authority_unverified`,
  `paid_billing_safe_handoff_authority_unverified`, and
  `encrypted_off_host_disaster_recovery_authority_unverified`. Play
  Closed Alpha is live in Austria; production access still needs 12 real
  opt-ins plus 14 continuous days. Dedicated Live billing credentials
  are absent and checkout is HTTP 503. Off-host DR has writable rclone
  remotes but no encrypted immutable restore path.
- Suggestion: Keep those fail-closed. Do not substitute rclone, pCloud,
  or a local dump for the DR contract. Do not turn billing on without
  the same-principal canary.
- Status: open

### Issue 7 -- Severity: suggestion

- File: `state/propertyquarry-dr/gnupg/private-keys-v1.d/`
- Description: A local GnuPG private keyring exists (mode `0600`) even
  though the handoff says the host has zero public recipients for backup
  encryption. That is a high-value secret on a shared 94%-full host, and
  it is not the approved external recovery recipient.
- Suggestion: Confirm whether this key is disposable lab material or a
  real recovery secret. If real, back it up off-host under the DR
  contract. If disposable, delete it after verifying it is not
  referenced by any receipt.
- Status: open

### Issue 8 -- Severity: suggestion

- File: `config/onemin_slot_owners.json`
- Description: The tracked file maps 19 1min slots to real owner names
  and emails (16 `@myexternalbrain.com`, 3 `@chummer.run`) plus
  `secret_sha256` values. `.gitleaks.toml` allowlists this path, so
  secret scanning will not catch future additions.
  `NEXT_SESSION_HANDOFF.md` also records personal Gmail addresses,
  Telegram message IDs, and the Play tester roster.
- Suggestion: Keep only hashed owner IDs in git. Move names and emails
  to a gitignored operator ledger. Stop appending personal emails to
  `NEXT_SESSION_HANDOFF.md`.
- Status: open

### Issue 9 -- Severity: suggestion

- File: `NEXT_SESSION_HANDOFF.md:273`
- Description: The Play tester source is a Google Group with anyone on
  the web able to see it and anyone on the web able to join. Membership
  is not the same as a Play opt-in, but a public join URL is an abuse
  path into Closed testing eligibility.
- Suggestion: Restrict join and visibility, or keep a private group plus
  the public opt-in URL. Do not treat group membership as the 12-tester
  count.
- Status: open

### Issue 10 -- Severity: suggestion

- File: `ea/app/product/service.py:1`
- Description: `service.py` is 70,725 lines. Neighboring tests include
  `tests/test_product_api_contracts.py` (37,581 lines) and
  `tests/test_propertyquarry_workspace_redesign.py` (32,556 lines). The
  private-showcase path, ranking, briefs, and large parts of the
  customer surface live in one file. That is not reviewable as a
  security unit.
- Suggestion: Split product surfaces (search, showcase, briefs, tours)
  before the next feature landing. Do not add more behavior to this
  file.
- Status: open

### Issue 11 -- Severity: suggestion

- File: `docs/REPO_ISOLATION.md`
- Description: Isolation correctly passes because quarantined EA and
  Chummer paths are not wired into PropertyQuarry Compose. They are
  still shipped in git: 181 `.codex-design/` files, 170 `feedback/`
  files, Chummer packet docs, `scripts/chummer6_guide_worker.py` (8,203
  lines), and `scripts/chummer6_guide_media_worker.py` (12,949 lines).
  Isolation only forbids deploy-path references.
- Suggestion: Move inherited archives out of the canonical product repo,
  or keep them but stop growing them. The isolation gate should also
  fail on new Chummer or EA authority files.
- Status: open

### Issue 12 -- Severity: suggestion

- File: `config/release/propertyquarry_repository_role.v1.json:3`
- Description: Policy names the canonical branch `main`. All current
  release evidence lives on `integration/property-origin-main-20260728`,
  306 commits ahead of `origin/main` (`e36feba9`). Dual remotes remain:
  `origin` and `propertyquarry` point at propertyquarry.git;
  `legacy-property` points at property.git.
- Suggestion: Either merge or fast-forward `main` to the integration
  envelope, or update the role policy to the real release branch. Do not
  leave `main` as a 306-commit-stale authority label.
- Status: open

### Issue 13 -- Severity: suggestion

- File: `docker-compose.yml:50`
- Also: `docker-compose.host-tools.yml:19`
- Description: Legacy EA Compose still uses `network_mode: host`.
  Host-tools mounts `/var/run/docker.sock`. These are not the
  PropertyQuarry runtime path. `docker-compose.property.yml` drops all
  capabilities, sets `no-new-privileges`, and does not mount the Docker
  socket. The host was still running a large EA stack beside
  PropertyQuarry, including `ea-api`, `ea-worker`, `ea-teable-relay`,
  `ea-fastestvpn-proxy-ch`, and a 2026-07-13 `ea-core-candidate` stack.
  The recent PayPal and PayFunnels inheritance bug came from this
  co-tenancy.
- Suggestion: Keep PropertyQuarry Compose isolated. Treat leftover EA
  candidate stacks as reclaimable. Do not run host-tools against the
  live production project.
- Status: open

### Issue 14 -- Severity: suggestion

- File: `vendor/propertyquarry-wheelhouse/`
- Also: `vendor/propertyquarry-python-wheels/`
- Description: Two overlapping wheel trees are tracked, about 104 MiB
  and 91 MiB, plus 114 Alpine APKs of about 54 MiB. Playwright appears
  twice at 47 MiB each. Supply-chain pinning is intentional; clone and
  review cost is not.
- Suggestion: Keep one authenticated wheelhouse. Deduplicate the second
  tree or generate it at bootstrap.
- Status: open

### Issue 15 -- Severity: nit

- File: `HANDOFF.md:1`
- Also: `NEXT_SESSION_HANDOFF.md:1`
- Description: `HANDOFF.md` is the 2026-08-04 Karl and Crezlo snapshot
  (180 lines). `NEXT_SESSION_HANDOFF.md` is a 2,259-line running log
  through 2026-08-13. Agents will read the stale file first.
- Suggestion: Make `HANDOFF.md` a short pointer to the current handoff,
  or delete it.
- Status: repaired; `HANDOFF.md` is now a short, non-authoritative pointer

### Issue 16 -- Severity: nit

- File: `state/artifacts/property-security-posture-current.json`
- Description: The static security-posture receipt is `status=pass`
  from 2026-07-01. Current web and render images are 2026-08-13. The
  receipt is not evidence for this candidate.
- Suggestion: Regenerate it against the current image IDs, or stop
  treating the July file as current.
- Status: open

### Issue 17 -- Severity: nit

- File: `mobile/capacitor.config.json:2`
- Description: The Android application ID is still
  `com.myexternalbrain.propertyquarry` while repo isolation forbids
  MyExternalBrain as release authority. Play listing and tester URLs
  are bound to that ID, so this is frozen for the current store app.
- Suggestion: Record it as an accepted store-identity exception, not as
  proof that MyExternalBrain is in the trust boundary.
- Status: repaired; `mobile/README.md` records the frozen store identity
  as outside the operational authority and trust boundary

## What looks solid

- No `pickle.loads` and no `shell=True` under `ea/`.
- No live `sk-`, `ghp_`, `AKIA`, or PEM material in tracked source.
  Matches were test sentinels only.
- `.env` is mode `0600` and gitignored. Example templates use
  placeholders. `.env.local.example` `DATABASE_URL` is the documented
  `local_dev_only` sentinel.
- Property Compose fail-closes on missing signing, OAuth, session, and
  render-bridge secrets. It blanks vendor 3DVista logins and Postgres
  owner passwords out of the API, pins PayPal and PayFunnels to
  dedicated Live inputs, and drops all capabilities.
- 345 `state/public_property_tours/**/tour.private.json` files are mode
  `0600`.
- Isolation script passes. Role script passes when the tree is clean.
- Billing remains HTTP 503 without Live admission. FlipLink remains
  `local_only`.
- Latest exact browser proof on the deployed candidate is recorded as
  96/96.

## Recommended order of work

1. Freeze other writers on this checkout. Resolve the dirty LTD files
   without mixing them into a silent audit commit.
2. `chmod 600` the world-readable Postgres dump. Rotate the BrowserAct
   owner token that leaked via `ps`.
3. Reclaim host disk: prunable worktrees, the July candidate stack, and
   unused images. Do not touch the current PropertyQuarry web or render
   images or the newest backup.
4. Remove hardcoded showcase emails from `ea/app/product/service.py`.
5. Keep the four public-launch blockers external and fail-closed.

## Extension — implementation, design, and LTD opportunities

Extended 2026-08-13 after comparing `LTDs.md`,
`docs/PRODUCT_BRIEF.md`, `docs/ROADMAP.md`, `docs/PRICING.md`,
`docs/PROPERTYQUARRY_WHOLE_PROJECT_SCOPE.md`,
`docs/PROPERTY_INTEGRATION_GOVERNANCE.md`,
`docs/propertyquarry_global_market_envelope.v1.json`, and the runtime
catalog in `ea/app/services/ltd_runtime_catalog.py` plus the customer
search, packet, billing, overlay, and integration code.

This extension does not reopen the host-security issues above. It asks
what the product is supposed to be, what the code actually does, and
which owned lifetime tools are sitting unused on the PropertyQuarry
customer path.

### Extension verdict

PropertyQuarry has a working Austria search-to-brief loop and one
verified customer LTD (`1min.AI` cover generation). Around that core
the design documents describe a larger product than the runtime sells,
and the LTD inventory describes a larger operator toolkit than the
customer ever sees.

The important split is not “LTDs exist / LTDs missing”. It is:

- **Design says one product.** The brief, roadmap, pricing page, whole-project scope, and market envelope disagree on geography, plans, billing owner, and gold.
- **Implementation has contracts for a second product.** Governance lanes, overlay tables, FlipLink share buttons, and a 16-country catalog are present. Almost all of them are disabled, local-only, fixture-backed, or operator-only.
- **LTDs are mostly EA/Chummer assets parked in this repo.** 58 products are tracked. The runtime catalog marks `discover_account` executable for every row. Only 1min has a PropertyQuarry customer-integration receipt.

Do not promote catalog executability, BrowserAct templates, or disabled
env flags into customer features. The highest-value unused LTD work is
the short list that completes the current Austria decision loop:
Emailit on `propertyquarry.com`, FlipLink family share, Rybbit/ClickRank
for this domain, Lunacal/MetaSurvey around viewings, Unmixr audio from
the existing brief, and Internxt as encrypted DR rather than a sync
mount.

### Design gaps

#### Issue 18 -- Severity: bug (repaired)

- File: `docs/PRODUCT_BRIEF.md:78` versus `docs/ROADMAP.md:7` versus `ea/app/services/property_market_catalog.py:174`
- Description: The product brief claims Austria, Germany, Switzerland, United Kingdom, Spain, Italy, France, Netherlands, and the United States as current flagship scope. The roadmap says Austria, Germany, and Costa Rica until those markets are reliable. The market envelope is an English-only invite-only private beta for AT/DE/CR and explicitly excludes a fully localized global product. The customer search allowlist is only `AT`, `DE`, `CR`. The same catalog still defines 16 countries including Belgium, Canada, Switzerland, Ireland, UK, Australia, Spain, Italy, France, Netherlands, Portugal, Poland, Sweden, and the United States. Costa Rica is in the customer allowlist but the envelope marks it browser-state-only, not a live provider journey.
- Suggestion: Keep Austria as the sole customer market until another market has comparable live-provider, localization, rights, and browser proof.
- Status: repaired; the customer picker is Austria-only and the wider catalog is explicitly future/operator-only

#### Issue 19 -- Severity: bug (repaired)

- File: `docs/PRICING.md:8` versus `ea/app/product/commercial.py:35` versus `ea/app/product/service.py:2941`
- Description: Customer pricing is Free / Plus (3 EUR) / Agent. Search and tour code enforce `free` / `plus` / `agent` (concurrent-search caps 1 / 2 / 4 in `property_surface_state.py:2347`). `commercial.py` still defines a different inherited model: Pilot / Core / Concierge with seats, messaging flags, and `/app/settings/plan`. ROADMAP still talks about Brilliant Directories as the billing skin. LTD and Compose treat PayFunnels as preferred and PayPal as fallback, both currently unconfigured. Testers see a fail-closed HTTP 503 billing page while docs and plan objects describe three different commercial products.
- Suggestion: Keep workspace collaboration modes explicitly non-authoritative and route all plan truth through the PropertyQuarry billing service.
- Status: repaired; inherited workspace modes no longer use customer plan/billing claims, and Free/Plus/Agent remain the only customer plans

#### Issue 20 -- Severity: suggestion (repaired)

- File: `docs/PRICING.md:13` versus `docs/ROADMAP.md:26`
- Description: Pricing says Free gets “1 to 2 high-level matches” and “shallow summary only”. The roadmap says every tier shows all available results ranked and that score is not a hide filter. The live Austria run persists 10 ranked candidates with opportunity briefs. Those three statements cannot all be true.
- Suggestion: Keep paid value in breadth, reruns, alerts, and research depth instead of silent score hiding.
- Status: repaired in pricing truth; Free explicitly shows the available ranked set and total

#### Issue 21 -- Severity: suggestion (repaired)

- File: `ea/app/services/ltd_runtime_catalog.py:364`
- Description: Static BrowserAct discovery and template contracts previously
  claimed `executable=True` without principal context. They now fail closed in
  the static catalog. The authenticated API projects them executable only when
  an enabled BrowserAct connector binding belongs to the requesting principal
  and explicitly includes the LTD service. Crezlo remains non-executable even
  with a binding because there is no customer-visible completion receipt.
- Suggestion: Mark `discover_account` executable only when a BrowserAct binding exists for that principal and service. Mark Crezlo executable only after a customer-visible completion receipt. Keep FlipLink non-executable until credentials exist, and change the button label to “Create local packet” until then.
- Status: repaired with shared binding-readiness logic and focused catalog tests; FlipLink remains non-executable and the customer actions save local packets

#### Issue 22 -- Severity: suggestion (repaired)

- File: `docs/PROPERTYQUARRY_WHOLE_PROJECT_SCOPE.md:9`
- Description: Whole-project gold requires canonical property identity, listing instances, claim-level evidence, change intelligence, viewing and offer capture, eight evidence overlays with Teable ingestion, Rybbit dashboard receipts, WCAG/visual CI, and Brilliant Directories as a non-authoritative handoff. The current shipped loop is still run-centric: search run, shortlist, research page, local brief, optional 1min cover, hosted Karl tour. The gold board is being used as if it were the current product definition.
- Suggestion: Split “Austria closed-test product” from “whole-project gold”. Keep gold as a backlog with fail-closed posture. Do not let overlay, directory, or change-intelligence work block the current search/brief/tour loop.
- Status: repaired; the scope document now begins with an Austria closed-test authority boundary and labels whole-project gold as future backlog unless promoted into the exact release manifest

#### Issue 23 -- Severity: suggestion (repaired)

- File: `docs/propertyquarry_global_market_envelope.v1.json:6`
- Description: The envelope is bound to candidate `fa7d2194` on 2026-07-19. Current HEAD is `2c9df6c4` and the deployed runtime is `0a44ea20`. AT content-locale, address, currency, WCAG, Firefox/Safari app, SEO hreflang, Core Web Vitals, provider-rights, and live-provider-e2e dimensions are `implemented_unproven`, `missing`, or `external_blocked`. CR evidence is a stale-terminal zero-result browser reconciliation, not a live Costa Rica search. The file still shapes release language.
- Suggestion: Keep the old evidence rows for provenance without letting them define current release scope.
- Status: repaired; the envelope is explicitly historical/superseded and names only AT as a current customer market

#### Issue 24 -- Severity: suggestion (repaired)

- File: `docs/ROADMAP.md:113` versus `docs/PROPERTY_INTEGRATION_GOVERNANCE.md:1`
- Description: ROADMAP wants Brilliant Directories visually skinned as `billing.propertyquarry.com`. Integration governance and `LTDs.md` forbid Brilliant Directories from owning billing, entitlements, ranking, or publication. The runtime lane is `directory_projection_disabled`. Two designs are still in the tree.
- Suggestion: Keep Brilliant Directories limited to a future non-authoritative directory projection.
- Status: repaired; roadmap and pricing name only PayFunnels/PayPal as checkout lanes and forbid Brilliant Directories billing authority

#### Issue 25 -- Severity: nit

- File: `docs/propertyquarry_global_market_envelope.v1.json:95`
- Description: The accessibility gate is documented as a contract whose “tri-engine test collection is fake” and that has no NVDA/JAWS/VoiceOver/TalkBack receipts. Dark mode exists only on admin content-studio templates, not on the customer workbench. ROADMAP requires dark mode on every surface.
- Suggestion: Either add a real customer dark theme and assistive-technology receipts, or remove dark mode and WCAG-certified language from gold claims.
- Status: open

### Implementation gaps

#### Issue 26 -- Severity: bug (repaired)

- File: `ea/app/templates/app/_property_workbench_script.html:2126`
- Description: “Share this home” and “Share results” POSTed a FlipLink-shaped packet (`fliplink_format: 'flipbook_3d'`) even though the deployed API has no FlipLink login, webhook, domain, or publication row. Packets stay `local_only` / `not_published`. The actions now say “Save review packet” and “Save shortlist packet”, their progress/error text is local, and their analytics event is `pq.packet.saved`.
- Suggestion: Keep FlipLink external publication disabled until the account, redaction, and first public read-back exist.
- Status: repaired with a focused customer-copy contract test; external FlipLink remains honestly unconfigured

#### Issue 27 -- Severity: suggestion

- File: `ea/app/product/property_search_schema.py:429`
- Description: Evidence overlay rollup and snapshot tables exist. The workbench already renders unavailable/stale/verified states for environment, heat, traffic, mobility, school, safety, media, and fiber. Whole-project gold says launch proof comes only from `scripts/property_evidence_overlay_read_model.py` through authenticated Teable tables. Teable remains an operator projection (`LTDs.md` Tier 2, not a PropertyQuarry customer integration). Live overlay cards will stay “Not yet” or will look verified from thin local facts.
- Suggestion: Keep the UI honest: default every overlay to unavailable until a current-candidate Teable-backed receipt exists. Do not treat OSM hints or listing adjectives as verified heat/fiber/safety.
- Status: open

#### Issue 28 -- Severity: suggestion

- File: `ea/app/services/property_integration_governance.py:61`
- Description: The priority integration lanes are all disabled: MetaSurvey and Lunacal `integrate_next_disabled`, ApiX-Drive `agent_beta_disabled`, Invoiless `commercial_ops_disabled`, Documentation.AI `docs_publishing_disabled`, Internxt `backup_pilot_disabled`, ApproveThis `agent_plan_pilot_disabled`, Unmixr `optional_prototype_disabled`, Brilliant Directories `directory_projection_disabled`, Sendr `outreach_lane_disabled`. The decision-state machine already knows `viewing_requested` and `offer_candidate`. Customers cannot schedule a viewing, send a post-viewing survey, export to a CRM, or get an invoice from those owned tools.
- Suggestion: Implement Lunacal and MetaSurvey next if the product is a decision loop. Leave Sendr, ApproveThis, and ApiX-Drive until Agent billing is real.
- Status: open

#### Issue 29 -- Severity: suggestion

- File: `ea/app/product/property_onemin_evaluation.py:47`
- Description: 1min already has two PropertyQuarry code paths. Cover generation is the verified customer integration. `PROPERTYQUARRY_ONEMIN_EVALUATION_ENABLED` defaults to false, so the 1min code-evaluate / Google-Maps OODA lane does not run. 70 worker slots exist; 45 were depleted and only 2 were composite-ready on 2026-08-12. The LTD catalog also advertises background-remove and upscale tools that the customer product never calls.
- Suggestion: Keep cover generation as the only customer 1min action until slot health is better. Do not enable evaluation or image mutation on listing photos without a rights and privacy receipt. If evaluation is useful, turn it on for Agent only and bound it to missing-fact questions, not a second ranking brain.
- Status: open

#### Issue 30 -- Severity: suggestion

- File: `README.md:32` versus `LTDs.md:21`
- Description: Emailit is workspace Tier 1, but the live proof is `chummer.run` sender-domain wiring. PropertyQuarry Compose and `.env.example` want `property@propertyquarry.com`. README still says the sender domain must be verified before that address can deliver. Saved-search alerts, registration mail, and billing mail therefore depend on a domain that is not the Emailit proof in `LTDs.md`. Heyy WhatsApp already has product routes and opt-out/budget guards, but Heyy is not in `LTDs.md` at all.
- Suggestion: Verify Emailit for `propertyquarry.com` or stop calling Emailit a PropertyQuarry Tier 1. Add Heyy to the LTD inventory with an honest integration tier. Do not promise recurring alerts until one customer delivery lane is proven on this domain.
- Status: open

#### Issue 31 -- Severity: suggestion

- File: `ea/app/services/public_clickrank.py:12` versus `LTDs.md:123`
- Description: ClickRank is live for `chummer.run` and `myexternalbrain.com`. The PropertyQuarry helper has a dedicated `CLICKRANK_AI_PROPERTYQUARRY_SITE_ID` slot and only allows public marketing paths. `.env.example` leaves that site ID empty. Rybbit has site id `10315` in the example file and is disabled by default (`PROPERTYQUARRY_RYBBIT_ENABLED=0`). Whole-project gold requires Rybbit dashboard receipts. Public SEO and analytics for this product are therefore designed, partially coded, and not evidenced on propertyquarry.com.
- Suggestion: Either bind ClickRank and Rybbit to propertyquarry.com with a privacy receipt, or remove the example site id and gold language until that receipt exists.
- Status: open

#### Issue 32 -- Severity: suggestion (repaired)

- File: `docs/PROPERTYQUARRY_SOURCE_OF_TRUTH_MAP.md:32`
- Description: Decision states include unseen, reviewing, shortlisted, blocked, needs_documents, needs_agent_answer, viewing_requested, offer_candidate, rejected, and archived. Feedback can move those states. There is no customer viewing calendar, document vault UI, offer tracker, or “what changed since last run” surface. Teable restore/portability scripts know `property_entities` / `listing_instances`, but the customer product is still a search-run workspace.
- Suggestion: Add a thin decision tray on the research page: Yes / Maybe / No, viewing requested, and “ask the agent” without waiting for Lunacal. Persist those events as the canonical memory. Layer scheduling and document research on top later.
- Status: repaired; the existing Yes/Maybe/No decision trail and persisted agent-question flow now have visible research actions for `viewing_requested` and `Ask the agent`, with a focused customer-copy contract

#### Issue 33 -- Severity: nit

- File: `ea/app/services/property_market_catalog.py:51`
- Description: Provider defaults are `market_readiness=private_beta`, `terms_review_status=needs_review`, `robots_review_status=needs_review`, `listing_cache_allowed=false`, `photo_republication_allowed=false`, `public_packet_allowed=false`, `maximum_concurrency=1`. ROADMAP wants four concurrent searches. The plan cap allows up to four Agent runs, but each provider is defined as serial. Live-provider e2e remains `external_blocked` in the market envelope.
- Suggestion: Keep concurrency honest: one in-flight fetch per provider, multiple runs queued. Do not advertise four parallel portal crawls until rights and rate limits say so.
- Status: open

### Missed product opportunities

These are useful product moves that do not require a new LTD.

#### Issue 34 -- Severity: suggestion

- File: `docs/PRODUCT_BRIEF.md:54`
- Description: The brief promises enrichment of heating, lift, transit, and family details, plus “which options deserve a real viewing”. Opportunity briefs now persist recommendation, fit, confidence, predicted reaction, trade-offs, and a listing link. They are `local_only`. There is no one-tap “prepare a viewing” pack (questions, unknowns, listing link, floorplan, tour) and no change-since-last-run line on a saved search.
- Suggestion: Promote the existing brief into a viewing sheet and a saved-search digest. That is the Free-to-Plus value without FlipLink or billing.
- Status: open

#### Issue 35 -- Severity: suggestion

- File: `docs/ROADMAP.md:87`
- Description: Tours are request-time, style-selected, with real progress. Karl has a licensed 3DVista control and a Crezlo tour that must not be re-created. The generic customer path still cannot request a 3DVista/Pano2VR/krpano export the way it can request a 1min cover. Crezlo is catalog-executable for operators, not a customer button.
- Suggestion: Add an explicit “Request a layout preview” versus “Request a licensed tour” split. Keep AI previews on 1min/render. Keep 3DVista as the branded viewer for accepted exports only. Do not put Crezlo on the customer request path until the completion receipt exists.
- Status: open

#### Issue 36 -- Severity: suggestion

- File: `docs/ROADMAP.md:21`
- Description: German and Spanish critical-shell localization exists; public, auth, account, billing, legal, provider, and dynamic content remain English. Play Closed testing is Austria. The store listing is German. The app the tester then uses is still largely English outside the search shell.
- Suggestion: Localize the signed-in search, shortlist, research, account, and billing-503 pages to de-AT before opening more markets. That is higher leverage than adding ES/IT/FR/NL/US catalog rows.
- Status: open

#### Issue 37 -- Severity: suggestion

- File: `ea/app/product/service.py:70725`
- Description: New product work keeps landing in one 70k-line module. Search, briefs, showcase, overlays, and plan limits cannot be changed safely. This is now a product-delivery gap, not only a reviewability gap.
- Suggestion: Extract briefs, showcase, and plan-limit enforcement before adding Lunacal, FlipLink live publish, or another market.
- Status: open

### Missed LTD integration opportunities

Owned tools that would complete the current PropertyQuarry loop, in recommended order. Do not wire the rest just because they are owned.

| Priority | LTD | Why it matters now | Current honest state | Do not do |
| --- | --- | --- | --- | --- |
| 1 | Emailit | Registration, alerts, and support mail on `property@propertyquarry.com` | Tier 1 for `chummer.run`, not proven for this domain | Reuse the Chummer sender as if it were PropertyQuarry |
| 2 | FlipLink.me | Family/agent share is the paid-adjacent output of the new briefs | Local packet only; UI now labels the action as save | Record a pasted URL as publication proof |
| 3 | Rybbit + ClickRank | Public conversion and AI-search presence for propertyquarry.com | Slots exist; ClickRank live only on other domains; Rybbit disabled | Send `/app` paths or listing URLs |
| 4 | Lunacal | Viewing/consult booking is the brief’s missing last step | Credentials only, lane disabled | Put exact addresses in public booking titles |
| 5 | MetaSurvey | Post-viewing / rejection reasons that feed ranking | BrowserAct results reader only | Let survey text rewrite listing facts |
| 6 | Unmixr | Audio of the existing redacted brief; 3M prebuilt credits already live in EA | Catalog-only for PropertyQuarry | Narrate raw listings or unpublished docs |
| 7 | Internxt | 100 TB already reachable; launch DR is the blocker | Plain rclone mount, not crypt/Object Lock | Upload plaintext dumps |
| 8 | 3DVista / Pano2VR / krpano | Karl already proves the viewer path | Tier 2; control-panel/export receipts still pending | Re-run Crezlo or treat AI panoramas as the licensed tour |
| 9 | Invoiless | Invoice/VAT once Plus exists | Commercial ops disabled; billing itself is 503 | Create invoices that grant entitlements |
| 10 | Documentation.AI | Public help and market guides for Austria testers | Username/password only | Ingest the repo, runbooks, or customer packets |
| 11 | Paperguide | Cited research on redacted public documents | Tier 3, no runtime | Send private PDFs or unredacted contracts |
| 12 | Teable | Designed home for overlay rollups | Operator projection / compaction design, not customer evidence | Treat Teable as listing truth |
| later | ApiX-Drive, ApproveThis, Sendr, Subscribr, Brilliant Directories, Deftform, NeuronWriter, ChatPlayground, MarkupGo, PeekShot, AvoMap, Jogg, MagicFit, VidBoard | Useful after Agent billing or for operator/newsroom work | Mostly credentials, BrowserAct templates, or Chummer/Fleet proof | Present any of these as a PropertyQuarry customer integration |

#### Issue 38 -- Severity: suggestion

- File: `LTDs.md:17`
- Description: 1min is the only row that `_PROPERTYQUARRY_CUSTOMER_LIVE_EVIDENCE` will accept. BrowserAct, Teable, ClickRank, Emailit, Unmixr, and Internxt can count as live evidence without being customer integrations. That distinction is correct. The missed opportunity is that several of those live-evidence services have an obvious customer job and are not queued as such: Emailit delivery, ClickRank/Rybbit public presence, Unmixr briefing, Internxt encrypted backup.
- Suggestion: Keep the verifier strict. Add an explicit “next customer LTD” queue of the four above, each with one receipt type, instead of refreshing the 58-row inventory.
- Status: open

#### Issue 39 -- Severity: suggestion

- File: `LTDs.md:52`
- Description: FlipLink is a stacked Tier 10 LTD bought for review packets, family flipbooks, QR, and later paid reports. The generator and privacy contract exist. The account does not. Every new brief makes this gap more visible because the product now has something worth sharing.
- Suggestion: Treat FlipLink credentialing and one redacted family-review read-back as the first post-billing product integration, or stop showing share CTAs.
- Status: open

#### Issue 40 -- Severity: suggestion

- File: `LTDs.md:81`
- Description: Unmixr has six usable accounts and large remaining credits, refreshed 2026-09-01. Governance already defines the safe input: redacted compiled dossier or approved public script. Opportunity briefs are now bounded, escaped, and local. That is exactly the input the Unmixr lane was designed for.
- Suggestion: Prototype Agent-only audio from the redacted brief with a transcript and “not a listing recording” disclosure. Keep `PROPERTYQUARRY_UNMIXR_ENABLED` off until the first human-reviewed receipt.
- Status: open

#### Issue 41 -- Severity: suggestion

- File: `LTDs.md:30`
- Description: Internxt is live as a storage principal and is already called out as insufficient for launch DR. Paperguide is the designed cited-document pilot. Using Internxt as a crypt remote for the existing dump+S3 design, or as the restore-drill target, is the LTD that unblocks a launch gate. Using it as “more cloud disk” is not.
- Suggestion: If an external GPG recipient appears, prefer Internxt crypt or the locked-S3 path, not another plain rclone canary.
- Status: open

#### Issue 42 -- Severity: suggestion

- File: `LTDs.md:19`
- Description: ChatPlayground, NeuronWriter, Poppy, Prompt Architects, Documentation.AI, and vexp are owned AI workbenches. The PropertyQuarry vexp *index* is healthy; this Grok session could not call it. See the vexp wiring section below. Prompt Architects is seeded but GM/runtime assist stays disabled. None of these should become customer chat. The missed opportunity is operator-side: one governed assist lane for missing-fact questions and help-center drafts, with PropertyQuarry remaining source of truth.
- Suggestion: Wire Grok to `vexp mcp --workspace /docker/property --proxy`. Keep customer UX non-chat. If an operator assist is added, bind it to Prompt Architects or 1min code, not a new ChatPlayground product surface.
- Status: open

#### Issue 43 -- Severity: nit (repaired)

- File: `LTDs.md`
- Description: Heyy WhatsApp is implemented in `ea/app/api/routes/heyy_integration.py` and `product_api.py` with opt-in, STOP, and daily template budget. It is absent from `LTDs.md`. FastestVPN has a Compose file in this repo while `LTDs.md` says it is not wired here. Answerly, Pixefy, Rafter, ProductLift, and Syllabbles are Chummer/Fleet gates living in the PropertyQuarry inventory.
- Suggestion: Add Heyy to the inventory. Move Chummer-only LTDs to a separate section so PropertyQuarry customer integration cannot be miscounted.
- Status: repaired; Heyy is tracked as a disabled/unproven Tier 2 customer lane, and the five named Chummer/Fleet-only rows are explicitly excluded from runtime-catalog and customer-integration counts

## Recommended product and LTD sequence

Use this after the host/security actions in the original list.

1. Resolve design contradictions in writing: AT private beta only; Free/Plus/Agent only; PayFunnels/PayPal only; Brilliant Directories is not billing.
2. Make share, overlays, and LTD catalog labels honest.
3. Localize the signed-in Austria loop to de-AT.
4. Turn the opportunity brief into a viewing sheet and a saved-search digest.
5. Prove Emailit on `propertyquarry.com`.
6. Credential FlipLink and publish one redacted family packet, or remove the share CTA.
7. Bind Rybbit/ClickRank to this domain with the existing privacy masks.
8. Add Lunacal + MetaSurvey only after a customer can mark `viewing_requested` inside PropertyQuarry.
9. Leave Unmixr, Invoiless, Paperguide, Documentation.AI, and Internxt crypt for the next slice. Do not start ChatPlayground, MagicFit, Jogg, Sendr, or Poppy as PropertyQuarry customer features.

## vexp wiring — Codex works, this Grok session does not

Looked at after the implementation/LTD extension. vexp is not down on
this host. The PropertyQuarry daemon and index are live. What failed is
only Grok’s MCP child.

### What is actually running

| Plane | State |
| --- | --- |
| Property daemon | PID 625048, workspace `/docker/property`, socket `/docker/property/.vexp/daemon.sock` connects, uptime about 6d 19h |
| Property index | 1,935 files, 31,014 nodes, 55,691 edges; `.vexp/healthy` current; `index.db` about 630 MiB |
| CLI from this checkout | `vexp daemon-cmd status` reports running. `vexp capsule` returns PropertyQuarry pivots, including `ea/app/services/ltd_runtime_catalog.py:_propertyquarry_customer_integration_verified` |
| Codex MCP | Works. `~/.codex/config.toml` runs `/home/tibor/.local/bin/vexp-codex-mcp` with `cwd = /docker/EA` and `VEXP_CODEX_DEFAULT_REPO=EA` |
| HTTP MCP `:7821` | Listening, Bearer-auth required. `vexp-http-supervisor.mjs` is pinned to `/docker/EA/.vexp/daemon.sock`, so this port is the EA gateway, not PropertyQuarry |
| Grok MCP | `grok mcp list` reports no servers in `~/.grok/config.toml`. This session still started a server named `vexp` via Claude compat and failed handshake |

CLI `vexp` is 2.5.1; 2.6.0 is available. VS Code has extensions 2.5.3 and
2.6.0. That version skew is not why Grok failed.

### Why Codex works

`/home/tibor/.local/libexec/vexp-codex-mcp.mjs` is a stable wrapper.
It does not start an in-process indexer. It waits for the existing
daemon socket and then execs:

```text
vexp mcp --workspace /docker/EA --proxy
```

That is why Codex can call `run_pipeline`. It is also why Codex vexp is
an **EA** context plane. PropertyQuarry is a second daemon in
`~/.vexp/daemons.json`. Codex does not attach to it.

### Why this Grok session failed

Grok loads MCP configs from `config.toml`, then Claude, then Cursor,
then `.mcp.json`. There is no `[mcp_servers.vexp]` in
`~/.grok/config.toml`. Claude compat supplied one from
`~/.claude.json`:

```json
"vexp": {
  "command": "node",
  "args": [
    "/home/tibor/.vscode-server/extensions/vexp.vexp-vscode-2.2.1-linux-x64/dist/mcp-server.cjs"
  ]
}
```

That 2.2.1 path is gone. The installed extensions are 2.5.3 and 2.6.0.
Grok spawned `node` on the missing file. The child exited with
`MODULE_NOT_FOUND`. The parent then reported:

```text
MCP server 'vexp' handshake failed: ... Broken pipe ... when send initialize request
```

The matching stderr is `~/.grok/logs/mcp/vexp.stderr.log`. This is not
an empty PropertyQuarry index and not a license failure.

### Cross-workspace noise

`/docker/property/.vexp/daemon.log` repeatedly warns that another
indexer (PID 60655) is already running on this workspace. PID 60655 is
the **EA** daemon (`--workspace /docker/EA`). Property still serves
queries, but file-sync on this checkout waits behind the EA indexer.
That is a host-hygiene defect, not the Grok handshake.

### What to change

Do not point Grok at `vexp-codex-mcp` or at `http://127.0.0.1:7821`.
Both are EA-scoped. The PropertyQuarry Grok MCP now lives in the
checkout at `.grok/config.toml`. It proxies this daemon with
`vexp mcp --workspace /docker/property --proxy`. Project scope beats
`~/.grok/config.toml` and Claude compat, so the stale 2.2.1 path is
replaced for sessions started in this repository.

A new Grok session is required; this session already finished the
failed handshake.

Also update or remove the Claude `mcpServers.vexp` entry so Claude
itself does not keep launching 2.2.1.

This session’s audit used grep/read because MCP was unavailable. The
index was queryable from the CLI the whole time.
