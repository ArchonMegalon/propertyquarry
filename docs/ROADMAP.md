# Roadmap

## Current Flagship Gold Goal

PropertyQuarry should feel like a premium product that just works for a normal
buyer, agent, or operator on desktop and phone. The current customer product is
the Austria closed test. Germany, Costa Rica, and the wider market catalog stay
future/operator-only until each market has live provider, localization, rights,
and browser evidence comparable to Austria.

### Product standard

- Every surface must be minimal, calm, and screen-space respectful, especially
  on mobile.
- The app must use human product language, not internal AI, crawler, score-gate,
  repair, or provider jargon.
- Anything non-obvious must have a short help tooltip, including climate-fit,
  provider coverage, 3D rendering, and billing handoff.
- Mobile must be presentation-ready across search, results, shortlist, research,
  property detail, account, billing handoff, history, and media-request flows.
- Dark mode, small phones, large phones, and desktop must all be readable and
  tappable with large touch targets and no clipped controls.

### Search and ranking

- Search must be market-aware: users cannot select a provider that does not
  serve the selected country or region.
- Default behavior for every tier is to show all available results ranked; score
  is for ranking, not for hiding homes.
- Hard filters must be strictly limited to true impossibilities such as price,
  market, property availability, and explicit user exclusions.
- Soft distance preferences must degrade smoothly: close matches get a boost,
  near misses lose the boost gradually, and far misses receive the full malus.
- Budget, district, radius, property-type, provider, and soft-filter changes must
  re-rank or rerun generically, not leave stale filtered/ranked counts.
- Search history gets a dedicated screen in the navigation; it must not be buried
  in a narrow side column.
- Search progress must be real: active providers, checked homes, current stage,
  repair status, ETA confidence, queue limits, and failure causes must reflect
  actual backend state.
- Up to four searches should run concurrently when capacity allows.

### Mobile search and districts

- Mobile search should use dedicated mobile flows, not squeezed desktop panels.
- Expanding one mobile section collapses the others.
- "What matters" should be fullscreen or sheet-based on mobile when needed to
  preserve vertical space.
- District selection offers either manual selection or map selection, not both at
  the same time.
- Map selection must use real OSM/admin district borders, support pan, one-tap
  zoom, two-finger zoom, and a closeable mobile popup so page scrolling does not
  break.
- Selected districts and any outside-border radius must render as one clear red
  covered search area.

### Results and research

- Results, shortlist, and research pages must fit the main decision on one
  screen whenever possible.
- Remove noisy sections such as generic "best so far", "run ranking", thin side
  decision wedges, and repeated evidence labels that do not add user value.
- Explain fit in concrete facts: nearest supermarket and distance, playground
  distance, school type distance, transit, heat resilience, nearby water, trees,
  and missing facts.
- Open-listing actions must work reliably.
- Users must be able to adjust min-score display/ranking behavior when a run has
  weak coverage, without pretending score is a hard filter.
- All provider mismatch, stale-count, and partial-coverage cases need a clear
  user-facing summary and a specific next action.

### Data enrichment

- Merge existing needs instead of inventing duplicate filters. Kindergarten,
  full-day primary school, half-day primary school, playground, daily life, and
  transit each need configurable desired distances where they make sense.
- Climate-fit must use real geo data where available: urban heat, air quality,
  shade, building/floor exposure, outside blinds, air conditioning, water bodies,
  cold-air corridors, vegetation, and dense urban heat islands.
- Additional overlays should be designed for results thumbnails and map detail:
  traffic density, fiber coverage, school catchment and pupil-flow data, crime
  where legally and ethically available, and newspaper mention statistics with
  links to original articles when still available.
- Heavy enrichment and newspaper indexing must be cached in Teable/geospatial
  stores so later searches are not slowed by re-indexing.
- The BTS/customer-facing PDF must explain where information comes from and how
  confidence, freshness, and missing data are handled.

### 3D tours and walkthroughs

- 3D tours and walkthroughs are created only after explicit user request.
- Style selection happens at request time for both 3D tours and walkthroughs,
  not inside search preferences.
- All tiers can choose all available styles at request time. Tier differences
  affect queue priority and included monthly/default style capacity, not whether
  an already-rendered style can be viewed.
- Free users should see longer render expectations when applicable; paid and
  agent users should see priority queue language backed by actual queue state.
- Real progress must be shown for 3D tours and walkthroughs: queued, assets
  received, floorplan/photo analysis, model generation, viewer packaging,
  upload, playable verification, and failure reason.
- 3DVista is the preferred branded viewer when ready. Matterport support must be
  included. krpano and Pano2VR are licensed fallback/export options, not a noisy
  visible downgrade.
- The old fallback tour pipeline must not surface to users.
- Walkthroughs should use the video render skill with 3D input and support
  style, day time, scenario, and optional uploaded people references.

### Auth, account, and billing

- Google or external-provider sign-in implicitly creates the account.
- Account pages must always offer an obvious logout action on mobile and desktop.
- Free, Plus, and Agent are the only customer plan names. Workspace modes are
  non-authoritative collaboration settings, not subscriptions.
- PayFunnels is the preferred checkout lane and PayPal the fallback. Both stay
  unavailable until dedicated Live credentials and the exact same-principal
  safe-handoff canary pass.
- Billing-provider events must be mapped fail-closed and can never grant or
  downgrade an entitlement without the local PropertyQuarry billing service.
- Agent lifetime users must remain authoritative in local auth and
  PropertyQuarry entitlement state regardless of external billing availability.
- Brilliant Directories may later project a public partner/agent directory; it
  must not own billing, entitlements, ranking, or publication.
- ID Austria can be added only if it improves trust without adding noisy or
  confusing sign-in lanes.

### Provider reliability and self-healing

- Provider failures should explain the likely cause in calm language, usually a
  changed or blocked provider page.
- If a provider cannot be repaired, skip it for the current run, mark it
  temporarily unavailable, and exclude it from future searches.
- Probe temporarily unavailable providers weekly for two months; if still broken,
  hide them from the search mask and mark them permanently unusable.
- Permanently unusable providers should still be probed monthly indefinitely.
- Search for new providers monthly and route discovered providers through the
  same catalog verification and market-scoping pipeline.

### Verification and release

- Production readiness requires targeted E2E tests that choose a known property,
  set filters that should find it, and prove the provider/search/ranking path
  returns it.
- Every active customer provider in Austria must be checked with targeted
  searches both with and without soft filters. Future-market catalog checks do
  not count as customer release evidence.
- Mobile visual tests, accessibility checks, dark-mode checks, and clipped-control
  checks are release gates.
- Billing handoff, entitlement sync, Google sign-in, account creation, logout,
  3D-tour request, walkthrough request, search history, and provider recovery all
  need live smoke receipts before a release is called gold.

## Milestone 1

Product shell extraction

- domain live
- new repo live
- brand and product language locked
- public landing and pricing defined

## Milestone 2

Search workspace productization

- preference onboarding
- country and language selection
- platform selection
- search-run progress
- shortlist and ranking

## Milestone 2a

International market coverage

- retain country-specific provider catalogs as non-customer research inventory
- admit a new customer market only after live provider, localization, rights,
  and browser receipts pass
- keep market-aware crawl entry URLs and provider-specific upgrades behind that admission gate

## Milestone 3

Research packets

- personalized property summary
- amenity and transit enrichment
- stronger missing-info investigation
- “request more details” flow

## Milestone 4

Commercialization

- free tier gating
- Plus tier
- Agent tier
- PayFunnels-based upgrade path

## Milestone 5

Feedback loop

- property like/dislike actions
- explain what is wrong with a listing
- preference learning
- better future ranking
