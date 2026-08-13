# PropertyQuarry Whole Project Scope

This is the future whole-product gold backlog for hardening passes and audits.
It is not the release definition for the current Austria closed test.

PropertyQuarry is a paid property decision product. A pass is not whole-project complete when it only improves one page, one provider, one visual component, or one happy path. It must check the customer journey, the operator control plane, the data lifecycle, and the safety boundaries that keep the product credible.

## Authority Boundary

The current customer product is the Austria closed-test search-to-brief loop:
sign in, set Austrian search preferences, run live-provider search, review all
available ranked results, persist decisions and opportunity assessments, open
research/tour evidence, and save a local review packet. Free, Plus, and Agent
are the only customer plans. Billing, external packet publication, additional
markets, and optional LTD lanes remain fail-closed until their own authority
receipts exist.

Everything below is a future gold backlog unless the release manifest names it
as a current-candidate gate. Missing gold-board overlays, directory projection,
or future-market evidence must not block an otherwise honest Austria closed-test
candidate. It also must not be presented as implemented customer functionality.

## Scope Rule

Whole-project work includes every system below:

1. Public entry and SEO surfaces.
2. Authentication, logout, account, sessions, data export, deletion, and share-link revocation.
3. Search setup, district and postal-code filtering, hard versus soft filter behavior, provider selection, and saved preferences.
4. Search execution, source coverage, fleet repair, retry state, ETA state, and interrupted-run recovery.
5. Results, filtered-breakdown actions, rank ordering, explanation quality, shortlist persistence, public sharing, and reruns.
6. Research detail, 360 tours, Matterport and 3DVista links, generated walkthrough requests, dossiers, videos, and missing-fact repair.
7. Automation and saved searches, including map thumbnails, edit/delete controls, delivery policy, and run history.
8. Provider governance, market readiness, rights, rate limits, cache policy, and provider-specific canaries.
9. Canonical property memory: property identity, listing instances, evidence claims, price and availability changes, documents, decisions, viewings, offers, and outcomes.
10. Ranking and learning: benchmark fixtures, hard-filter violation rate, soft-filter score impact only, feedback loops, and model-version receipts.
11. Notifications, scout thresholds, email and WhatsApp delivery governance, unsubscribe/STOP handling, and delivery receipts.
12. Billing, invoices, VAT, refunds, entitlements, plan limits, credit usage, and commercial lifecycle copy.
13. Privacy, prompt-injection boundaries, public-tour manifests, public assets, retention, exports, deletion, backups, and restore drills.
14. Accessibility, responsive layout, keyboard navigation, focus state, contrast, reduced motion, and screen-reader labels.
15. Observability: SLOs, structured logs, queue depth, provider success, cost per run, incident signals, and live smoke checks.
16. Documentation, help center, legal pages, provider attribution, generated-tour disclaimers, and localization.
17. Integration governance for LTD/provider lanes such as Subscribr, MetaSurvey, ApiX-Drive, Invoiless, Lunacal, Documentation.AI, Paperguide, Internxt, ApproveThis, Unmixr, and Brilliant Directories.
18. Optional Brilliant Directories directory projection, with PropertyQuarry retaining identity, plan, invoice, entitlement, access-check, ranking, publication, and customer-data source of truth.
19. Documentation.AI/release-audit governance, including authoritative release manifest, branch/deployment reconciliation, security posture, reproducible builds, CI gates, documentation separation, and current-HEAD proof receipts.

## Definition Of Done

A whole-project pass must produce at least one of these outcomes for every touched area:

- a user-visible fix;
- a state-machine or storage fix;
- a privacy, security, or rights boundary;
- a focused unit or e2e test;
- a release-gate check;
- a documented backlog item with owner, evidence gap, and fail-closed posture when implementation is larger than the current pass.

Audit prose alone is not done.

## Product Tone

The customer-facing product must be quiet, premium, specific, and property-first. Generic assistant, memo, office, queue, handoff, and operator vocabulary belongs only in internal infrastructure or quarantined archives. Customer surfaces should speak in property terms: searches, sources, listings, homes, dossiers, tours, decisions, viewings, documents, alerts, and shared results.

## Additional Goal

The additional whole-scope goal is to keep moving PropertyQuarry from run-centric search toward durable property intelligence:

- one canonical property identity across duplicate or relisted provider entries;
- claim-level evidence and freshness;
- change intelligence since the last run;
- viewing and outcome capture;
- benchmarked ranking and repair behavior;
- governed provider and content integrations.

This goal remains active until those systems are implemented, tested, and visible in the relevant customer or operator surfaces.

## Future Gold Board Extensions

The future gold board includes these extensions. They become release gates only
when promoted into an exact-candidate release manifest:

- Billing must feel premium on desktop and mobile: current plan, checkout, upgrade, downgrade, cancellation, renewal, failed-payment recovery, invoice history, refunds, VAT/tax copy, entitlement state, and support path must be visible without breaking the search workflow.
- Brilliant Directories can be used only as a governed white-label directory or payment/account handoff lane. It cannot own billing truth, entitlements, provider access, private user profile data, property facts, ranking, search scope, or publication approval.
- Any Brilliant Directories webhook is advisory until signature verification, replay protection, receipt logging, and local entitlement reconciliation pass.
- Documentation.AI/audit intake is part of the release gate. A gold claim requires an authoritative release manifest, clean branch/deployment mapping, hardened Docker/runtime posture, reproducible build proof, CI/security/accessibility/visual gates, and current-HEAD receipts.
- The documentation.ai audit is an active blocker list, not background context. Each P0 item must be fixed, moved to a tracked release blocker with owner/evidence, or explicitly declared out of scope for the PropertyQuarry runtime plane before any gold claim.
- The current release manifest lives in `docs/PROPERTYQUARRY_RELEASE_MANIFEST.md`; it must be updated for every pushed/deployed candidate and must state whether the deployed artifact is gold, working-candidate, or blocked.
- Mobile phone UI is a first-class release surface across search, shortlist, research, billing, account, alerts, settings, authentication, and public conversion pages. A desktop-only pass is incomplete.
- Mobile app navigation must stay top-only and consistent across authenticated surfaces. Legacy bottom docks, duplicated launch buttons, clipped navigation pills, and sub-44px coarse-pointer targets are release regressions, not design variants.
- Search district selection must become a premium map-first mobile surface: large municipality preview, visible district borders, pan/zoom, tap-to-add districts, semi-transparent red selected overlays, and an accessible list fallback. No overlap, clipped controls, tiny targets, or scroll traps are acceptable on phone.
- Result thumbnails must evolve into a premium evidence-map viewer when clicked. The viewer should support verified overlays for environmental quality, summer heat, traffic/noise, public mobility, school context, official aggregate safety context, media-attention frequency, and fiber/broadband coverage, with provenance, freshness, uncertainty, and chart receipts visible before any layer is treated as factual.
- Evidence-map overlays must never fake coverage. Unverified layers render as unavailable or experimental with source requirements, not as decorative heatmaps. Official aggregate safety context must be framed as area context rather than property/person scoring. Media-attention layers must show source links, topic labels, and time windows rather than a vague “good/bad neighborhood” score. Newspaper/media statistics must support opening the original article page when the source URL is still available and terms permit it, with clear unavailable/archived/source-removed states when an article cannot be opened.
- Fiber/broadband overlays must separate official coverage data from provider-specific address checks. Mine official grid/area data first, normalize it to the property coordinate or district polygon, show available technologies, maximum advertised speed bands, fixed/mobile distinction, and named ISPs only when the source permits it. Provider availability checks are secondary verification jobs with receipts, rate limits, and terms-safe automation; OSM/infrastructure tags may only be weak hints, never confirmation.
- Evidence-map performance is a gold requirement. Newspaper, environmental, safety, school, mobility, and fiber ingestion must run as asynchronous indexing jobs into Teable-backed geographic evidence tables, then into compact Postgres/read-model rollups where needed. Search execution may read only cached rollups by property coordinate, street, district, or search polygon; it must not crawl, parse, or re-index newspaper archives or provider coverage sites inline. Every overlay card must distinguish cache recency from source publication/observation time or reference period, expose the Teable table/source receipt, and retain stale/unavailable states. Follow-up searches must stay within route/search performance budgets even when evidence indexing is cold.
- Launch proof for those overlays is produced only by `scripts/property_evidence_overlay_read_model.py`: it reads all eight registered tables through the authenticated Teable API with redirect, response-digest, table-identity, and pagination evidence; atomically replaces the indexed Postgres rollup under a transaction lock; benchmarks three lookups per layer against the fixed 100 ms ceiling; and writes a private candidate-bound receipt. Production customer requests use only that Postgres read model and degrade to explicit unavailable states. A registry fixture, local JSON rollup, source-code check, or producer `status=pass` alone has no launch authority.
- Mobile search must treat district selection as a premium, low-motor-effort surface: map mode and manual list mode are either/or, mobile map interaction happens inside a dismissible dialog, real OSM-derived district borders are used where available, and Risk/Evidence controls use severity/evidence-level choices rather than vague check prompts.
- Mobile gold proof must include a current live research detail route, not only `/app/research`. The release gate must smoke a real `/app/research/{id}?run_id=...` page for compact research layout, visual/tour controls, no fake 3D readiness, and decision/workbench density before the mobile UI can be presented as ready.
- Whole-project gold must include implemented, customer-visible evidence overlays, not only scope text. At minimum, environmental quality, summer heat, traffic/noise, public mobility, school context, official aggregate safety context, media-attention statistics with article links, and fiber/broadband coverage must each have a source registry, Teable ingestion table, cached read model, UI unavailable/stale/verified state, and performance receipt proving searches do not index those sources inline.
- The evidence overlay source/layer contract lives in `docs/PROPERTYQUARRY_EVIDENCE_OVERLAY_REGISTRY.json` and is enforced by `scripts/check_property_whole_project_scope.py`.
- Cache/source/reference-period semantics live in `docs/PROPERTYQUARRY_EVIDENCE_OVERLAY_TEMPORAL_CONTRACT.md`; cache recency must never be presented as source freshness.
- Whole-project gold must include Rybbit dashboard receipts, not only app-side event hygiene. Public conversion, authenticated product engagement, billing handoff, tour/walkthrough interaction, support/recovery, and search activation funnels need dashboard or API evidence that events arrive under the approved taxonomy without candidate IDs, listing URLs, signed URLs, emails, phone numbers, or raw notes.
- The protected Rybbit producer is `scripts/propertyquarry_rybbit_evidence.py`. It must load the exact public candidate in a real browser, observe a non-empty tracking script, emit one anonymous attribute-free launch probe, capture the collector POST/2xx response before browser shutdown, and confirm site identity, has-data state, and event arrival through authenticated Rybbit APIs. Gold consumes the fresh receipt by candidate SHA, public/analytics origins, and hashed site identity; markup attributes and performance-smoke taxonomy checks remain supplemental.
- Whole-project gold must include continuous release gates for visual quality and accessibility, not only focused smoke tests. CI or release scripts must include browser screenshot baselines, axe/accessibility scans, keyboard/focus checks, high-zoom/mobile checks, empty/error/loading state coverage, and performance budgets for first-value journeys.
- Whole-project gold must include production hardening receipts for runtime security, supply chain, and authorization: non-root/default no-host-Docker posture, locked dependencies and pinned images, dependency/container scans, SBOM, durable workspace RBAC/session revocation, key rotation posture, and proof that loopback no-auth/principal-header overrides are disabled in production.

## Documentation.AI Future-Gold Audit Intake

The documentation.ai whole-project audit remains future-gold input. Current
release blockers must be copied into the authoritative release manifest rather
than inferred from this backlog.

P0 items that block a gold claim:

- Establish one authoritative release manifest with repository, branch, commit SHA, deployment endpoint, public origin, artifact set, and release label.
- Reconcile public branch, local branch, deployed commit, and receipt commit before regenerating or relying on new release receipts.
- Keep host-level Docker control out of default public runtime containers; Docker socket, host workspace mounts, and root runtime privileges belong only in explicit operator profiles.
- Make builds reproducible with locked dependencies, pinned browser/runtime artifacts, pinned base images, dependency audit, container scan, and SBOM evidence.
- Replace config-derived operator privilege with durable workspace membership, role assignment, scoped grants, session revocation, issuer/audience checks, key versioning, and security-event receipts.
- Keep loopback no-auth and principal-header overrides disabled for production.

P1 items that remain active until fixed or explicitly scoped out:

- Split deployment profiles so core PropertyQuarry runtime, provider labs, media workers, experimental integrations, and operator tooling do not share one attack surface.
- Add hard CI gates for type/lint, security scanning, dependency/container audit, visual regression, accessibility, coverage, release freshness, and markdown-link validity.
- Remove public host-port exposure from production defaults; ingress should run through the authenticated proxy or tunnel with trusted-proxy configuration.
- Separate customer/product documentation from operator/provider/internal mechanics.
- Add responsive visual stability, keyboard, screen-reader, high-zoom, empty/error/loading, large-data, and mobile usability proof for first-value journeys.

## Brilliant Directories Directory Projection Goal

Brilliant Directories is limited to a future governed public partner/agent
directory projection. It is not a checkout, subscription, billing, invoice, or
entitlement lane. PropertyQuarry remains source of truth.

Required behavior before promotion:

- PropertyQuarry owns account identity, plan, invoice display, entitlement checks, refunds, cancellation, renewal, failed-payment recovery, support state, and agent-tier unlimited behavior.
- Brilliant Directories may expose only allowlisted public directory/profile links and signed advisory webhook notifications.
- Webhooks must have signature verification, replay protection, receipt logging, idempotency, and local entitlement reconciliation before they can affect any user-visible billing or access state.
- Every billing state must have a mobile-safe local fallback that keeps the user on PropertyQuarry when the handoff is unavailable, unsigned, replayed, misconfigured, or returns a non-allowlisted URL.
- Billing receipts must avoid credentials, payment secrets, raw webhook bodies with private data, and provider-owned customer truth.

### Current Long-Running Flagship Goal (active)

The authoritative active objective is defined in
`docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md`. For the current release slice,
bring the preserved candidate to an honest Austria closed-test standard: close
safe local product, accessibility, localization, privacy/security, reliability,
operations, provenance, and customer-journey gaps; then obtain exact-candidate
browser, production-like, protected-live, and physical-device evidence. Wider
markets remain future backlog. No local source, generated receipt, unsigned
self-assertion, or historical live observation may be relabelled as launch
authority.

The goal continues until the terminal acceptance contract in that document is
met for one exact clean candidate or an irreducible credential, legal/business,
external-service, or explicit-authority blocker is evidenced precisely. Work may
continue through safe local implementation, tests, builds, and read-only
inspection. Pushing, publishing, purchasing, deploying, changing external
services, activating traffic, or weakening a safeguard requires explicit user
authority and the applicable independent controller.

For every continuation pass, the objective is:

1. Remove noise, internal vocabulary, redundant hops, fake readiness, and cramped mobile layouts wherever they appear.
2. Keep search semantics correct: hard filters constrain eligibility, soft filters rank, district/postal scope is country-safe, and all tiers see ranked results by default.
3. Keep provider execution, repair, quarantine, ETA, progress, and targeted-search E2E proof truthful for Austria; Germany, Costa Rica, and other catalog markets remain non-customer backlog until admitted separately.
4. Keep 3D tours and walkthroughs request-driven, style-aware, vendor-real, browser-rendered, and free of user-facing provider/internal labels.
5. Keep billing and account access single-sign-on quality: a signed-in PropertyQuarry user must not be asked to create a second account or complete an avoidable second login.
6. Keep release governance strict: documentation.ai P0/P1 findings, runtime security, reproducible builds, visual/accessibility gates, Rybbit receipts, and release-manifest freshness are active blockers until proven or explicitly scoped out.

Acceptance criteria before this pass is considered complete:

- Provider/source counters are always rendered as true provider count + checks, never with source-variant counts mislabeled as providers.
- Explicit selected district and postal constraints remain hard filters unless intentionally marked fuzzy/adjacent mode.
- Logout works for browser and API-driven sessions.
- Run status/repair messaging stays truthful when repair tasks are queued and when repair completes.
- Scope/automation thumbnails render usable district overlays without clipping key shapes, and map preview payloads reject unsupported image pipelines.
- Billing and Brilliant Directories states either pass the governed handoff contract or fail closed on a PropertyQuarry-owned recovery surface.
- Signed-in billing handoff must prove either an active external account session, a signed member-login-token handoff, or a locally owned recovery surface. Guided second-login assist is not a flagship-ready billing handoff.
- The documentation.ai audit has a current release-manifest receipt with repository, branch, commit, deployment endpoint, artifact set, verification commands, and unresolved blockers.
- Default runtime security, reproducibility, CI, auth, and public-network findings from the audit are either fixed in code or tracked as explicit P0/P1 release blockers.
- Every above is covered by unit/e2e test and a smoke check.
- Live mobile smoke for gold must be configured with `PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE` pointing at a currently valid research detail URL. If no current detail route is available, the correct status is blocked, not mobile-ready.
