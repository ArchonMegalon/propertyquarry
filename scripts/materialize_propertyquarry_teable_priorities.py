#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_ENV_FILES = (
    Path("/docker/property/.env"),
    Path("/docker/EA/.env"),
)

TABLE_NAME = "propertyquarry_product_priorities"

FIELDS = [
    {"name": "projection_id", "type": "singleLineText", "unique": True},
    {"name": "priority", "type": "singleLineText"},
    {"name": "area", "type": "singleLineText"},
    {"name": "title", "type": "singleLineText"},
    {"name": "status", "type": "singleLineText"},
    {"name": "user_visible", "type": "checkbox"},
    {"name": "owner_lane", "type": "singleLineText"},
    {"name": "current_state", "type": "longText"},
    {"name": "next_action", "type": "longText"},
    {"name": "source", "type": "singleLineText"},
    {"name": "updated_at", "type": "singleLineText"},
]

PRIORITIES = [
    {
        "projection_id": "pq-priority-search-location-hard-filters",
        "priority": "P0",
        "area": "Search correctness",
        "title": "Postal-code and district hard filters must never leak wrong areas",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "search-runner/provider-adapters",
        "current_state": (
            "Verified: title, summary and URL postal conflicts override dirty source-scope placeholders before matching, "
            "ranking and notification. Source-scope extraction normalizes all postal URL/label scopes, strips URL path tails "
            "like `/augasse`, and listing text now preserves user-facing `Wien` labels while matching still handles aliases."
        ),
        "next_action": (
            "Keep broad live-provider canaries for Austrian postal-code/province slug cases and expand fixtures when new "
            "provider-specific location encodings appear."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-soft-filters-score-only",
        "priority": "P0",
        "area": "Ranking",
        "title": "Soft filters affect score, not eligibility",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "ranking/e2e",
        "current_state": (
            "Verified in unit and e2e coverage: non-hard daily-life preferences preserve the discovered hit set, while "
            "soft mismatches only score-demote, annotate distance preference notes and affect ordering/explanation."
        ),
        "next_action": (
            "Keep the soft-filter equivalence E2E in the release gate and extend it with every new What matters category."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-repair-fleet-durable",
        "priority": "P0",
        "area": "Reliability",
        "title": "Repair workflow must be executable and durable",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "fleet/job-system",
        "current_state": (
            "Provider repair tasks, stale-run replacement, worker-exception repair, repair receipts and generic "
            "retry-budget quarantine receipts are executable. Compact run snapshots now preserve replacement run links, "
            "repair receipts, task counts and can-auto-repair state. The repair fleet canary now proves a deferred "
            "source-fetch repair advances to completed_partial with a quarantine receipt instead of stale queued copy."
        ),
        "next_action": (
            "Keep scripts/propertyquarry_repair_fleet_canary.py and the focused repair lifecycle tests in the release gate; "
            "add provider-specific live canaries when each high-volatility source gets a stable fixture."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-ui-minimal-polish",
        "priority": "P0",
        "area": "UX polish",
        "title": "Every surface must be minimal, readable and purposeful",
        "status": "in_progress",
        "user_visible": True,
        "owner_lane": "frontend/design-system",
        "current_state": (
            "Customer-facing security copy no longer uses review-gate/visual-check proof wording. A static surface gate now "
            "catches known noise phrases, unsafe hash links, unnamed buttons and missing image alt attributes across the "
            "main PropertyQuarry public, app, result, research, account and billing templates."
        ),
        "next_action": (
            "Keep removing oversized panels and repeated status rows; add screenshot coverage for landing, search, results, "
            "research, agents, automation, account, sign-in, pricing and legal pages in light/dark/mobile."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-search-wizard-feedback",
        "priority": "P0",
        "area": "Search workflow",
        "title": "Search wizard navigation and launch feedback must be immediate",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "frontend/search",
        "current_state": (
            "Browser tests verify step clicks replace visible controls without accumulation, step changes scroll back to "
            "the wizard nav, the top launch button remains visible, and launch shows a busy/disabled state immediately."
        ),
        "next_action": (
            "Keep these browser tests in the release gate whenever the search setup layout is rearranged."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-dark-light-design-tokens",
        "priority": "P0",
        "area": "Design system",
        "title": "Light and dark mode must share readable component tokens",
        "status": "in_progress",
        "user_visible": True,
        "owner_lane": "frontend/design-system",
        "current_state": (
            "Sign-in provider rows now use compact shared surface/icon/button tokens and no longer show noisy Google?/Facebook? "
            "help buttons. Dark-mode shared overrides now cover dynamic event, source, route-preview, result-panel, "
            "summary-link, textarea, table and action-card surfaces. Broader screenshot coverage is still required."
        ),
        "next_action": (
            "Replace remaining one-off page-specific panels with shared tokens and screenshot-test landing, sign-in, account, "
            "search, results, research, agents, automation and pricing in both themes."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-automation-map-thumbnails",
        "priority": "P1",
        "area": "Automation",
        "title": "Automation cards use OSM district-overlay thumbnails only",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "frontend/maps",
        "current_state": (
            "Verified: automation cards use the map-only preview builder, reject generic local thumbnail/point-preview "
            "fallbacks, materialize async OSM district overlays, and keep a small framing margin around selected shapes."
        ),
        "next_action": (
            "Keep map-only preview tests in the release gate and extend fixtures when new countries add district boundary data."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-tour-walkthrough-explicit-request",
        "priority": "P1",
        "area": "Tours and media",
        "title": "360 tours and walkthrough renders must be request-driven",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "media-factory",
        "current_state": (
            "Verified: generated visual requests send auto_deliver=false, keep buttons disabled while queued, expose "
            "ready walkthrough links only after completion, reject Willhaben tracking endpoints as provider 360 URLs, "
            "and preserve Matterport live embeds without leaking private source URLs in public manifests."
        ),
        "next_action": (
            "Keep the manifest-backed sent-link browser test for live Matterport/3DVista and flythrough URLs; it skips "
            "locally unless PROPERTYQUARRY_SENT_LINKS_MANIFEST points at real sent links."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-account-lifecycle",
        "priority": "P1",
        "area": "Account lifecycle",
        "title": "Account data controls need export, deletion, sessions and shared-link revocation",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "account/privacy",
        "current_state": (
            "Verified: account exposes export, search-history clear, access links, connected services, analytics/learning, "
            "delete-data, public packet/tour management and no-store export responses. Packet archive/dashboard, public-tour "
            "auth gates, retention docs and deletion page are covered by focused tests."
        ),
        "next_action": (
            "Keep account lifecycle, packet archive, public-tour auth and retention-document tests in the release gate; "
            "extend them when document vault or full account-deletion automation lands."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-public-tour-manifest",
        "priority": "P1",
        "area": "Privacy/security",
        "title": "Public tour manifests must be positive-schema safe at rest",
        "status": "verified",
        "user_visible": False,
        "owner_lane": "public-tours/security",
        "current_state": (
            "Verified: raw tour.json is built through PublicTourManifest from a narrow top-level allowlist, "
            "listing/property/source URLs and brief are excluded, private source fields are written only to "
            "PrivateTourReceipt, served assets are manifest-bound, the Crezlo public-tour publisher now uses the same "
            "manifest builder instead of hand-writing broad tour.json payloads, and focused public-tour privacy/live-tour "
            "tests pass."
        ),
        "next_action": (
            "Keep the manifest contract check, raw tour.json privacy tests, Crezlo publisher privacy test, asset-suffix "
            "tests and public action auth gates in the release gate; remove legacy compatibility wrappers in a later "
            "cleanup pass."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-provider-rights-readiness",
        "priority": "P1",
        "area": "Provider governance",
        "title": "Provider rights and market-readiness registry",
        "status": "verified",
        "user_visible": False,
        "owner_lane": "provider-governance",
        "current_state": (
            "Verified: provider specs carry explicit access mode, API/browser access posture, terms and robots review "
            "status, cache policy, media/public-packet rights, attribution, concurrency, request-rate, owner and market "
            "readiness metadata. Search source specs and provider options expose the governance snapshot, and the release "
            "gate now fails if new providers violate the rights/readiness contract."
        ),
        "next_action": (
            "Keep scripts/check_property_provider_governance.py in the release gate; only promote a provider or market to "
            "public after terms, robots, rights review timestamps and provider canaries are recorded."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-payfunnels-commercial-lifecycle",
        "priority": "P1",
        "area": "Billing",
        "title": "Finish PayFunnels commercial lifecycle",
        "status": "in_progress",
        "user_visible": True,
        "owner_lane": "billing/payments",
        "current_state": (
            "Pricing has been simplified; PayFunnels completion webhooks are idempotent; failed, cancelled and refunded "
            "callbacks now clear stale pending checkouts and record bounded billing event receipts; the billing surface "
            "shows compact latest-payment and billing-history rows only when useful, preserves PayFunnels invoice IDs as "
            "accounting handoff receipts with invoice/VAT fields, refunded PayFunnels payments now revoke the paid entitlement instead of "
            "leaving access active, and the release gate now runs the full PayFunnels checkout/webhook/refund/mismatch "
            "contract subset."
        ),
        "next_action": (
            "Finish cancel/downgrade policy automation, full accounting-lane invoice/VAT documents, failed-payment recovery "
            "actions and production PayFunnels smoke receipts."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-signin-logout-account-minimal",
        "priority": "P1",
        "area": "Account and auth",
        "title": "Sign-in, logout and account surfaces must be fast and minimal",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "auth/account-ux",
        "current_state": (
            "Verified: authenticated root keeps /?home=1 as the public-home escape, disabled Facebook sign-in stays hidden, "
            "Google uses compact identity-only copy, logout clears all workspace-session cookie variants including localhost/"
            "domain/secure cases, and account exposes working lifecycle controls without inherited EA noise."
        ),
        "next_action": (
            "Keep the auth/account focused tests and real-browser logout smoke in the release gate; re-run them when root, "
            "sign-in, account navigation or connected identity providers change."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-results-recovery-controls",
        "priority": "P1",
        "area": "Results recovery",
        "title": "Filtered counts must open useful relaxation controls",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "results/rerun-ux",
        "current_state": (
            "Verified: filtered-count affordances open a compact recovery dialog or fallback breakdown, empty-result desks "
            "show counterfactual recovery actions, and recovery rows include sliders, adjustment payloads and newly-ranked "
            "estimate text."
        ),
        "next_action": (
            "Keep the filtered-link browser tests and template contract tests in the release gate; add provider-specific "
            "recovery fixtures whenever a new hard-rule bucket is introduced."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-run-progress-eta-minimal",
        "priority": "P1",
        "area": "Run progress",
        "title": "Run progress and ETA must feel alive without noise",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "run-state/frontend",
        "current_state": (
            "Verified: live run pages auto-poll without manual refresh controls, progress stays at 0 during bootstrap, "
            "advances only after real source output, ETA is recorded after source progress, repair copy uses compact "
            "PropertyQuarry language, and empty-result desks open useful recovery actions."
        ),
        "next_action": (
            "Keep the progress/ETA invariants, no-refresh template test and active-run browser smoke in the release gate; "
            "extend them for new terminal or repair states."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-provider-source-labels",
        "priority": "P1",
        "area": "Run state",
        "title": "Provider and source-variant counts must be accurate",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "run-state/provider-catalog",
        "current_state": (
            "Verified: service and UI regressions collapse source variants to real provider brands, repair inflated "
            "provider totals from stored run summaries, and prevent the old 156-provider/source-variant wording from "
            "appearing in run-health and no-result summaries."
        ),
        "next_action": (
            "Keep provider-total and source-variant regression tests in the release gate; extend the same provider/source "
            "distinction to any new management surfaces."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-scout-notification-quality",
        "priority": "P1",
        "area": "Notifications",
        "title": "Scout updates only send strong, valid matches",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "notifications/scout",
        "current_state": (
            "Verified: scout notifications strip search-scope labels for all postal scopes, enforce a hard 60/100 outbound "
            "floor even if env is misconfigured lower, and suppress wrong-area, generic-page and low-score candidates before "
            "ranking/notification."
        ),
        "next_action": (
            "Keep broad live-provider canaries for wrong-area candidates and include concise source/listing links only when "
            "the candidate is eligible."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-performance-first-load",
        "priority": "P1",
        "area": "Performance",
        "title": "First-load performance for app, agents and research pages",
        "status": "in_progress",
        "user_visible": True,
        "owner_lane": "frontend/performance",
        "current_state": (
            "Root and app landing were improved. scripts/propertyquarry_authenticated_performance_smoke.py now seeds an "
            "authenticated local workspace, saved agents, and a synthetic completed run, then enforces first-paint route "
            "budgets for search, agents, properties, shortlist, research, account and billing without hitting providers. "
            "The operator route-budget override now applies to default routes too, and the failure path is tested with "
            "an intentionally strict 1 ms budget."
        ),
        "next_action": (
            "Add a Docker/live authenticated variant and keep heavy media/research sections lazy so route budgets stay "
            "stable when real run payloads grow."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-research-page-premium-performance",
        "priority": "P1",
        "area": "Research detail",
        "title": "Research pages must load fast and fit the decision workflow",
        "status": "in_progress",
        "user_visible": True,
        "owner_lane": "research/frontend",
        "current_state": (
            "The authenticated performance smoke now renders a seeded research detail page with a ranked candidate and "
            "checks it stays under budget with explicit media-request actions. The visual layout still needs broader "
            "screenshot tightening on dense real packets."
        ),
        "next_action": (
            "Slim the research layout to one-screen decision density, lazy-load expensive media/state, keep 360 first, "
            "and add screenshot tests for real dense packets."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-mobile-pwa",
        "priority": "P1",
        "area": "Mobile",
        "title": "Mobile and PWA posture",
        "status": "in_progress",
        "user_visible": True,
        "owner_lane": "frontend/mobile",
        "current_state": (
            "The product uses one responsive app, has mobile browser coverage for public home, workspace, What matters, "
            "running progress, setup controls and public tours, and now exposes a stronger PWA manifest with stable app "
            "id, standalone scope, launch handler, maskable icon and Search/Results/Saved-search shortcuts."
        ),
        "next_action": (
            "Add more mobile screenshot smoke for account, billing, agents and dense research detail; validate any "
            "future offline viewing-notes scope before caching private data."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-prompt-injection-boundary",
        "priority": "P1",
        "area": "Security",
        "title": "Untrusted listing and document content is data, never instruction",
        "status": "in_progress",
        "user_visible": False,
        "owner_lane": "security/research-pipeline",
        "current_state": (
            "Listing-text extraction strips scripted/hidden content and flags instruction-like text. Content-studio source "
            "packets and generated drafts now fail validation on prompt-injection language and hidden/scripted markup."
        ),
        "next_action": (
            "Extend the same boundary to uploaded PDF metadata/OCR, external feeds and every LLM research prompt; keep "
            "malicious fixtures in release gates and record prompt/schema versions."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-legal-trust-pages",
        "priority": "P1",
        "area": "Trust and legal",
        "title": "Public trust, legal, attribution and disclaimer pages",
        "status": "verified",
        "user_visible": True,
        "owner_lane": "public/trust",
        "current_state": (
            "Public Privacy, Terms, Imprint, Support, Cookies, Subprocessors, Refunds and Disclaimers pages render, are "
            "in the sitemap, are linked from the public footer, and are covered by the live public smoke. The copy keeps "
            "generated-tour, investment, provider-rights and data-lifecycle boundaries explicit."
        ),
        "next_action": (
            "Keep these pages reviewed by a qualified owner before public paid launch and update them when billing, "
            "provider-rights or subprocessors materially change."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-browser-security-smoke",
        "priority": "P1",
        "area": "Security",
        "title": "Public and app entry surfaces must ship browser security headers",
        "status": "verified",
        "user_visible": False,
        "owner_lane": "security/deploy-smoke",
        "current_state": (
            "The public live smoke now fails if HTML-like public routes or the app auth boundary lose the expected "
            "Content-Security-Policy, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, or HTTPS HSTS posture. "
            "The deploy script already runs this smoke against the target base URL."
        ),
        "next_action": (
            "Keep the security-header smoke in every deploy gate and extend it to new public/app entry routes before they "
            "are linked from navigation, SEO, pricing or account surfaces."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-accessibility-release-gate",
        "priority": "P1",
        "area": "Accessibility",
        "title": "Accessibility must become a release gate",
        "status": "in_progress",
        "user_visible": True,
        "owner_lane": "frontend/accessibility",
        "current_state": (
            "scripts/check_property_surface_accessibility.py is wired into property_release_gates.sh and guards main "
            "PropertyQuarry templates against known noise copy, unsafe/empty links, unnamed buttons and images without alt "
            "attributes. It now also rejects unnamed dialog surfaces, requires reduced-motion and visible-focus "
            "primitives, enforces shared touch-target/coarse-pointer tokens, and checks core light/dark text contrast pairs "
            "on the public shell, app shell, workbench and research detail templates."
        ),
        "next_action": (
            "Add axe and keyboard-flow browser checks; include PDF language, "
            "heading order, bookmarks and accessible link labels for generated dossiers."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-external-investment-feed-hardening",
        "priority": "P1",
        "area": "Investment data",
        "title": "External investment feeds need host allowlist and protected cache",
        "status": "done",
        "user_visible": False,
        "owner_lane": "investment/security",
        "current_state": (
            "External investment feeds now require an allowed-host configuration for HTTPS, only allow local insecure "
            "HTTP by explicit env, default to /docker/property/state, cap response size and chmod cache files to 0600."
        ),
        "next_action": (
            "Keep the focused feed-hardening tests in the release gate and add production source freshness/attribution "
            "receipts before surfacing investment outputs broadly."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-content-studio-subscribr",
        "priority": "P2",
        "area": "Content studio",
        "title": "Subscribr content studio with source-bound script packets",
        "status": "open",
        "user_visible": False,
        "owner_lane": "content-studio/integrations",
        "current_state": (
            "Subscribr should be an operator-governed video-script and content pre-production lane, not a property truth, "
            "ranking, billing or publication authority."
        ),
        "next_action": (
            "Implement source-packet contracts, privacy/fair-housing/freshness validation, Subscribr receipts, human review "
            "and direct-publish disabled gates before any live listing content is used."
        ),
        "source": "Subscribr integration guide",
    },
    {
        "projection_id": "pq-priority-ltd-integration-roadmap",
        "priority": "P2",
        "area": "Integration roadmap",
        "title": "Governed LTD integrations that fill missing product systems",
        "status": "open",
        "user_visible": False,
        "owner_lane": "integration-governance",
        "current_state": (
            "The next LTD work should add product systems rather than more generators: MetaSurvey, Lunacal, ApiX-Drive, "
            "Invoiless, Documentation.AI, Paperguide, Internxt, ApproveThis and Unmixr only under boundaries."
        ),
        "next_action": (
            "Implement one shared adapter contract with allowed data classes, kill switches, receipts and delete behavior; "
            "start with MetaSurvey/Lunacal and ApiX-Drive/Invoiless."
        ),
        "source": "LTD integration audit",
    },
    {
        "projection_id": "pq-priority-property-passport",
        "priority": "P2",
        "area": "Product moat",
        "title": "Canonical property passport and change intelligence",
        "status": "open",
        "user_visible": True,
        "owner_lane": "property-memory",
        "current_state": (
            "The product is still run/candidate-centric. Durable value comes from one property identity that accumulates "
            "listings, claims, documents, media, decisions, viewings and outcomes."
        ),
        "next_action": (
            "Introduce property_entities, listing_instances, property_claims, property_events, property_documents, "
            "property_decisions and viewing/outcome states; build 'what changed since last review'."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-search-run-tenancy-schema",
        "priority": "P1",
        "area": "Data model",
        "title": "Search-run tenancy and schema versioning",
        "status": "in_progress",
        "user_visible": False,
        "owner_lane": "storage/security",
        "current_state": (
            "Composite principal/run primary-key migration and service-level principal scoping are present. The release "
            "gate now validates source-level tenancy invariants even without DATABASE_URL, including composite upsert, "
            "principal-scoped load/delete, no owner mutation and no empty-principal user listing."
        ),
        "next_action": (
            "Finish optimistic versions or event tables for concurrent provider/ranking/research updates, and keep the live "
            "Postgres primary-key/index check active in deploy environments."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-dedicated-worker-queues",
        "priority": "P1",
        "area": "Reliability",
        "title": "Dedicated worker queues for provider, research, render and delivery lanes",
        "status": "in_progress",
        "user_visible": False,
        "owner_lane": "fleet/job-system",
        "current_state": (
            "A canonical PropertyQuarry worker-queue catalog now defines provider-fetch, browser-extraction, evidence, "
            "ranking, research, document, PDF, tour/media, notification, projection-sync and repair lanes. Provider repair "
            "tasks/events now carry the stable repair queue lane and queue budget metadata."
        ),
        "next_action": (
            "Back the catalog with durable job rows, locks, retry leases, checkpoints and dead-letter receipts for each lane "
            "so slow browser extraction, rendering and delivery cannot starve each other."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-notification-governance",
        "priority": "P1",
        "area": "Notifications",
        "title": "Email, Telegram and WhatsApp delivery governance",
        "status": "open",
        "user_visible": True,
        "owner_lane": "delivery",
        "current_state": (
            "A canonical delivery-governance catalog now covers email, Telegram and WhatsApp, including verified "
            "destinations, opt-in, quiet hours, receipts, suppression, and WhatsApp STOP/START posture. The Alerts "
            "surface now renders these rules as a first-class PropertyQuarry page instead of redirecting to Account. "
            "The release gate now also runs Heyy WhatsApp adapter/API contracts for opt-in, STOP/START, webhook-secret "
            "validation, redacted receipts and property/search-agent template sends."
        ),
        "next_action": (
            "Back the delivery-governance catalog with durable channel tables, email bounce/complaint handling, "
            "webhook receipts, per-channel retention controls, and unified editable user delivery preferences."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-ranking-benchmark",
        "priority": "P1",
        "area": "Ranking",
        "title": "Offline ranking and research benchmark",
        "status": "in_progress",
        "user_visible": False,
        "owner_lane": "ranking/evaluation",
        "current_state": (
            "An executable offline benchmark now guards exact postal/district hard filters, score-only soft filters, "
            "top ordering and scout notification thresholds. Broader relevance/outcome replay is still open."
        ),
        "next_action": (
            "Expand the benchmark into versioned briefs, larger candidate sets, missing-fact truth, outcome fixtures, "
            "explanation-faithfulness checks and cost-per-useful-shortlist deltas."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-market-readiness-localization",
        "priority": "P1",
        "area": "Markets",
        "title": "Market readiness, currency, timezone and localization",
        "status": "open",
        "user_visible": True,
        "owner_lane": "market-catalog/localization",
        "current_state": (
            "The catalog exposes many countries and languages, but not every market has equal provider rights, evidence "
            "coverage, currency formatting, timezone behavior, documents, localized notifications and support readiness."
        ),
        "next_action": (
            "Add market states from catalog_only to public, require provider canaries and rights review before public "
            "exposure, and introduce Money, localized dates/numbers, market/user timezones and address formats."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-billing-invoice-vat-lifecycle",
        "priority": "P1",
        "area": "Billing",
        "title": "Invoice, VAT, cancellation, refund and failed-payment lifecycle",
        "status": "open",
        "user_visible": True,
        "owner_lane": "billing/accounting",
        "current_state": (
            "PayFunnels activation, failure, refund and invoice-id webhook receipts are implemented; refund callbacks now "
            "revoke paid access and billing history can show compact invoice/VAT handoff state. The commercial lifecycle still "
            "needs real accounting document generation, failed-payment recovery automation and cancel/downgrade policy."
        ),
        "next_action": (
            "Keep PropertyQuarry as entitlement truth, use PayFunnels/PayPal only for payment proof, add Invoiless or an "
            "accounting lane for invoice/VAT documents, and expose compact billing history without operational noise."
        ),
        "source": "whole-product audit",
    },
    {
        "projection_id": "pq-priority-observability-dr",
        "priority": "P2",
        "area": "Operations",
        "title": "Observability, SLOs and restore drills",
        "status": "open",
        "user_visible": False,
        "owner_lane": "ops",
        "current_state": (
            "Logs and container health are not enough for a paid product. Search duration, provider coverage, queue age, "
            "render success, notification success and restore ability need measurable proof."
        ),
        "next_action": (
            "Add SLO dashboards, provider canaries, queue-depth alerts, encrypted backups, artifact backup and regular "
            "restore drills with RPO/RTO."
        ),
        "source": "whole-product audit",
    },
]


def _load_env_files(*paths: Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in loaded:
                loaded[key] = value.strip().strip("'").strip('"')
    return loaded


def _env_value(name: str, defaults: dict[str, str], fallback: str = "") -> str:
    return str(os.environ.get(name) or defaults.get(name) or fallback).strip()


def _request_json(
    *,
    method: str,
    url: str,
    api_key: str,
    body: dict[str, object] | None = None,
) -> object:
    data = None if body is None else json.dumps(body, ensure_ascii=True).encode("utf-8")
    try:
        request_timeout = max(5.0, float(os.environ.get("PROPERTYQUARRY_TEABLE_REQUEST_TIMEOUT_SECONDS") or 30.0))
    except Exception:
        request_timeout = 30.0
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://app.teable.ai",
            "Referer": "https://app.teable.ai/",
            "User-Agent": "PropertyQuarryTeablePriorityMaterializer/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:1000]
        raise SystemExit(f"HTTP {exc.code} from Teable: {detail}") from exc
    except Exception as exc:
        raise SystemExit(f"Teable request failed: {exc}") from exc
    if not payload.strip():
        return {}
    try:
        return json.loads(payload)
    except Exception as exc:
        raise SystemExit(f"Teable returned invalid JSON: {exc}") from exc


def _items(payload: object, key_names: tuple[str, ...]) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in key_names:
            rows = payload.get(key)
            if isinstance(rows, list):
                return [dict(item) for item in rows if isinstance(item, dict)]
    return []


def _extract_id(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("id", "tableId"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        for key in ("table", "data"):
            value = _extract_id(payload.get(key))
            if value:
                return value
    return ""


def _list_tables(*, base_url: str, api_key: str, base_id: str) -> dict[str, str]:
    payload = _request_json(
        method="GET",
        url=f"{base_url}/api/base/{urllib.parse.quote(base_id)}/table",
        api_key=api_key,
    )
    tables: dict[str, str] = {}
    for item in _items(payload, ("tables", "data", "items")):
        name = str(item.get("name") or item.get("tableName") or "").strip()
        table_id = str(item.get("id") or item.get("tableId") or "").strip()
        if name and table_id:
            tables[name] = table_id
    return tables


def _ensure_table(*, base_url: str, api_key: str, base_id: str) -> tuple[str, bool]:
    tables = _list_tables(base_url=base_url, api_key=api_key, base_id=base_id)
    existing = str(tables.get(TABLE_NAME) or "").strip()
    if existing:
        return existing, False
    payload = _request_json(
        method="POST",
        url=f"{base_url}/api/base/{urllib.parse.quote(base_id)}/table/",
        api_key=api_key,
        body={"name": TABLE_NAME, "fields": FIELDS, "fieldKeyType": "name"},
    )
    table_id = _extract_id(payload)
    if not table_id:
        raise SystemExit(f"Teable create-table response did not include a table id for {TABLE_NAME}")
    return table_id, True


def _existing_records(*, base_url: str, api_key: str, table_id: str) -> dict[str, str]:
    found: dict[str, str] = {}
    skip = 0
    take = 1000
    while True:
        query = urllib.parse.urlencode(
            {
                "fieldKeyType": "name",
                "cellFormat": "json",
                "take": take,
                "skip": skip,
                "projection": "projection_id",
            }
        )
        payload = _request_json(
            method="GET",
            url=f"{base_url}/api/table/{urllib.parse.quote(table_id)}/record?{query}",
            api_key=api_key,
        )
        records = _items(payload, ("records", "data", "items"))
        for record in records:
            fields = dict(record.get("fields") or {})
            projection_id = str(fields.get("projection_id") or "").strip()
            record_id = str(record.get("id") or "").strip()
            if projection_id and record_id:
                found[projection_id] = record_id
        if len(records) < take:
            break
        skip += take
    return found


def _upsert_rows(*, base_url: str, api_key: str, table_id: str, rows: list[dict[str, object]]) -> tuple[int, int]:
    existing = _existing_records(base_url=base_url, api_key=api_key, table_id=table_id)
    created = 0
    updated = 0
    pending_creates: list[dict[str, object]] = []
    for row in rows:
        projection_id = str(row.get("projection_id") or "").strip()
        if not projection_id:
            raise SystemExit("priority row missing projection_id")
        record_id = str(existing.get(projection_id) or "").strip()
        if record_id:
            _request_json(
                method="PATCH",
                url=f"{base_url}/api/table/{urllib.parse.quote(table_id)}/record/{urllib.parse.quote(record_id)}",
                api_key=api_key,
                body={
                    "fieldKeyType": "name",
                    "typecast": True,
                    "record": {"fields": row},
                },
            )
            updated += 1
        else:
            pending_creates.append({"fields": row})
    for start in range(0, len(pending_creates), 50):
        chunk = pending_creates[start : start + 50]
        payload = _request_json(
            method="POST",
            url=f"{base_url}/api/table/{urllib.parse.quote(table_id)}/record",
            api_key=api_key,
            body={"fieldKeyType": "name", "typecast": True, "records": chunk},
        )
        records = _items(payload, ("records", "data", "items"))
        created += len(records) or len(chunk)
    return created, updated


def parse_args() -> argparse.Namespace:
    defaults = _load_env_files(*DEFAULT_ENV_FILES)
    parser = argparse.ArgumentParser(description="Materialize key PropertyQuarry product priorities into Teable.")
    parser.add_argument("--base-url", default=_env_value("TEABLE_BASE_URL", defaults, "https://app.teable.ai"))
    parser.add_argument("--api-key", default=_env_value("TEABLE_API_KEY", defaults))
    parser.add_argument("--base-id", default=_env_value("PROPERTYQUARRY_TEABLE_BASE_ID", defaults))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = str(args.base_url or "https://app.teable.ai").strip().rstrip("/")
    api_key = str(args.api_key or "").strip()
    base_id = str(args.base_id or "").strip()
    if not api_key:
        raise SystemExit("missing TEABLE_API_KEY")
    if not base_id:
        raise SystemExit("missing PROPERTYQUARRY_TEABLE_BASE_ID")
    table_id, created_table = _ensure_table(base_url=base_url, api_key=api_key, base_id=base_id)
    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = [{**row, "updated_at": updated_at} for row in PRIORITIES]
    created, updated = _upsert_rows(base_url=base_url, api_key=api_key, table_id=table_id, rows=rows)
    print(
        json.dumps(
            {
                "status": "ready",
                "table_name": TABLE_NAME,
                "table_id": table_id,
                "created_table": created_table,
                "created_count": created,
                "updated_count": updated,
                "row_count": len(rows),
                "priority_counts": {
                    priority: sum(1 for row in rows if row.get("priority") == priority)
                    for priority in sorted({str(row.get("priority") or "") for row in rows})
                },
                "updated_at": updated_at,
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
