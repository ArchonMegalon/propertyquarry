# PropertyQuarry Product Release Checklist

## Flagship closeout rule

This checklist applies only to the standalone PropertyQuarry product. Inherited
`EA` names are compatibility and governance inputs; they do not make Executive
Assistant office workflows part of the release.

A source-level pass is not a production-launch claim. The candidate must also
satisfy `.codex-design/repo/IMPLEMENTATION_SCOPE.md`,
`.codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md`,
`.codex-design/ea/START_HERE.md`,
`.codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json`, and
`.codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json`, alongside the
candidate-bound browser receipt, `RELEASE_CHECKLIST.md`, and the protected
live-release gates. Every receipt must name the same product and exact candidate
identity.

## Product boundary

- The public and authenticated surfaces identify only PropertyQuarry.
- The release proves the property search-to-decision loop; it does not require
  Executive Assistant memo, inbox, commitment, handoff, or people workflows.
- Provider, listing, media, billing, and publication claims remain attributed,
  uncertainty-aware, consent-bound, and receipt-backed.
- Operator, provider, and compatibility vocabulary does not leak into customer
  copy or navigation.
- Public traffic promotion remains unavailable outside the governed controller.

## Activation and account

- `/` states the current product promise without legacy or side-brand drift.
- A new user can sign in, create or reuse a property brief, choose a market and
  providers, and reach the first useful result without configuring messaging.
- Account, settings, pricing, plan, usage, and billing-handoff surfaces use
  customer-safe language and fail closed when an external handoff is unusable.
- Authentication, principal isolation, entitlement, logout, and session-return
  behavior are proven on desktop and mobile.

## Search-to-decision loop

- A saved brief can dispatch a real or explicitly governed search and preserve
  market, currency, provider, budget, and nearby-distance context.
- Live run state updates without losing the selected run or property on refresh.
- Ranked results remain visible below preference thresholds unless a hard rule
  excludes them, and each decision reason is understandable.
- A user can inspect a property dossier, source links, missing facts, risks,
  research status, and the next useful action.
- Shortlist, compare, feedback, preference learning, and revisit flows persist
  and render the saved result after reload.
- Empty, delayed, partial, failed, offline, and retry states preserve truthful
  progress and an actionable recovery path.

## Packets, tours, and media

- Hosted packets and public pages expose only explicitly published, redacted
  property evidence and retain source attribution.
- Spatial tours and generated media are provider-safe, quota-aware, mobile
  usable, keyboard accessible, and backed by current receipts.
- Missing premium media, WebGL, image decode, or provider availability falls
  back to a useful property view without fabricating proof.
- Public publication, republishing, notifications, and destructive lifecycle
  actions require the documented consent and authority.

## Quality, privacy, and reliability

- Accessibility, cognitive load, contrast, focus, 400% reflow, reduced motion,
  and mobile layout gates pass on the promoted candidate.
- Chromium, Firefox, and WebKit prove exact routes, visible decoded images,
  bounded first value, loading, offline, recovery, and stable layout behavior.
- Privacy lifecycle, retention, authenticated metrics, structured logging,
  correlation, alerts, provider/quota posture, and customer-safe errors pass.
- PostgreSQL schema, migration, scheduler, delivery-outbox, concurrency,
  backup/restore, host recovery, and rollback contracts remain fail closed.
- Dependency audit, SBOM, image scan, secret posture, and release provenance bind
  to immutable candidate images in the installed controller's protected
  security lane.

## Automated and live gates

- `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py property-release-gates` passes.
- `make ci-gates` passes, including runtime API and release-asset verification.
- PropertyQuarry browser, activation, accessibility, failure-state, security,
  continuous-UX, PostgreSQL, and product E2E jobs pass on the exact candidate.
- `make verify-flagship-release-readiness` passes against current generated
  receipts rather than hand-edited evidence.
- The installed v2 controller's flagship security admission passes with
  immutable web and render images. The disabled legacy
  `propertyquarry-flagship-security` workflow job is migration history, not
  release evidence; a skipped or missing controller security phase is not a
  green production result.
- Controller preflight, migration, observability, disaster recovery, live
  activation-to-value, Gold, and rollback evidence all bind to the promoted
  candidate before public traffic changes.
- Public `/health/ready`, `/version`, authenticated journeys, mobile journeys,
  provider posture, and rollback readiness are re-proved after promotion.

## Completion condition

Any unavailable, degraded, preview-only, or operator-dependent capability is
named in the release manifest and customer experience. PropertyQuarry is live
only when terminal hosted CI, protected production gates, controller receipts,
public runtime evidence, and the verified rollback path agree.
