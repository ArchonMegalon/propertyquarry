# PropertyQuarry local repository audit

- **Observed at**: 2026-08-13
- **Checkout**: `/docker/property`
- **Branch**: `integration/property-origin-main-20260728`
- **Upstream**: `origin/integration/property-origin-main-20260728`
- **HEAD**: `2c9df6c41f08d83570c01424f57591a27192b588` (`docs(release): record final property audit evidence`)
- **Scope**: independent local audit of the checkout, git tree, host operator plane, and running runtime
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
- Status: open

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
- Status: open

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
