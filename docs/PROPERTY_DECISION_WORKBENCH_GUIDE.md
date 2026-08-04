# PropertyQuarry Decision Workbench Guide

This guide defines how to redesign PropertyQuarry from the current routed workspace into a modern, low-noise, paid-product decision app.

It is intentionally implementation-oriented. The goal is not a prettier version of the existing console. The goal is a property analyst workbench where search, ranked results, 360 evidence, OODA, investment research, comparison, and feedback stay in one focused workflow.

## 1. Product Direction

PropertyQuarry should feel like a paid property decision desk, not an inherited assistant console.

The primary user job is:

1. define or reuse a search brief
2. start or watch a search
3. inspect ranked results in a table
4. select one property
5. see the 360 first
6. understand why it matched
7. inspect OODA, risks, investment signal, and evidence
8. compare against nearby alternatives
9. give feedback and move on

The main product should therefore behave as one app surface, even if individual routes still exist for deep links and fallback rendering.

## 2. Target Interaction Model

### 2.1 Primary Screen

The core authenticated screen is a three-zone workbench:

- **Left drawer:** search brief, filters, providers, saved profile, plan limits
- **Center:** ranked results table and live run state
- **Right pane:** selected property dossier

The user should not need to bounce through `Search`, `Shortlist`, `Research`, and `Profile` as separate mental places. Those routes can remain, but the default work surface should hold the whole decision context.

### 2.2 Route Strategy

Keep these stable routes:

- `/app/properties`
- `/app/properties?run_id=<id>`
- `/app/research/<candidate_ref>?run_id=<id>`
- `/app/billing`
- `/app/settings`

Change the default experience:

- `/app/properties` renders the new decision workbench.
- `/app/research/<candidate_ref>` renders the same dossier model as a full-page deep link.
- Existing `Shortlist`, `Research`, `Profile`, and `Alerts` routes become compatibility/deep-link views, not the main user journey.

### 2.3 SPA-Like Behavior Without Premature Framework Rewrite

Do not begin by moving to React/Vue/Svelte. First prove the interaction model with the existing server-rendered app plus a thin client layer.

Required behavior:

- selected row updates the right dossier without a full page transition
- live run polling updates the central state in place
- search brief opens in a drawer and preserves current results
- feedback updates the visible candidate and profile summary in place
- browser refresh preserves `run_id` and selected candidate through URL state

Only consider a dedicated frontend framework after this behavior is proven and gated.

## 3. Visual Direction

### 3.1 Tone

PropertyQuarry should feel quiet, utilitarian, expensive, and analytical.

Use:

- dense but readable tables
- restrained color
- clear hierarchy
- fewer panels
- stable table columns
- precise status badges
- strong evidence surfaces

Avoid:

- nested cards
- decorative gradients
- marketing copy inside the app
- every section looking equally important
- large hero blocks inside the authenticated product
- clickable-looking static decoration
- inherited assistant terminology

### 3.2 Recommended Layout

Desktop, 1440px and wider:

- left drawer: `320px`
- center results: `minmax(560px, 1fr)`
- right dossier: `420-520px`
- global top bar: compact, single row
- persistent run/status strip: above results table

Tablet:

- left drawer collapses to a filter button
- center results remains primary
- dossier opens as a right overlay or stacked panel

Mobile:

- top segmented mode switch: `Results`, `Property`, `Brief`
- results become compact rows
- selected property opens as a full-screen panel
- 360 remains first in the property panel

## 4. Information Architecture

### 4.1 Global App Chrome

The app chrome should contain only:

- brand
- current workspace/user
- current plan
- search/run status
- settings/billing access

Primary navigation should be reduced to:

- `Results`
- `Property`
- `Brief`
- `Settings`

Do not expose generic EA or operator concepts in the customer product.

### 4.2 Search Brief

The search brief should be compact and collapsible once a run exists.

Controls:

- country
- region/state
- city/district cluster
- `All of Vienna` toggle for Vienna
- listing mode: rent/buy
- property type
- budget
- rooms
- area
- priorities multi-select
- provider multi-select
- investment research mode
- result cap

Behavior:

- active run hides the full brief
- finished run defaults to results only
- user can reopen the brief from a clear edit button
- changed brief must show unsaved state before launching again

### 4.3 Results Table

The results table is the product center.

Required columns:

- rank
- 360
- match
- title/location
- price
- price per sqm
- area/rooms
- yield or rent estimate for buy/investment mode
- OODA
- risk
- status

Rules:

- first visible column after rank is 360/tour status
- row click selects the property
- primary action opens/updates the dossier
- source portal link is secondary
- duplicate listings are collapsed
- no finished run should show the full search form above the result table

### 4.4 Property Dossier

The property dossier is the premium object.

Order:

1. 360/tour region
2. OODA summary
3. why selected
4. risks and missing facts
5. investment research, if buy mode and allowed
6. key listing facts
7. neighbourhood facts
8. comparison table
9. evidence and provenance
10. raw/source links

The user should understand within five seconds:

- what this is
- why it was selected
- whether there is a 360
- what the main upside is
- what the main risk is
- whether it deserves action

### 4.5 360 State Model

The dossier must always have a 360 region, even when no tour is ready.

States:

- `ready`: embedded or linked tour is available
- `queued`: tour generation is queued
- `processing`: tour generation is running
- `blocked`: source data cannot support a tour
- `missing`: source did not expose usable media yet

Each non-ready state must show:

- status
- ETA if known
- what will happen next
- whether the user can still review the packet

### 4.6 OODA

OODA should be concrete, not generic.

Required rows when data is available:

- nearest playground
- nearest pharmacy
- nearest supermarket
- nearest underground/tram/train
- commute or bike estimate
- local risk/development signals when researched

OODA summary should be above the fold in the dossier:

- `Observe`: facts and neighbourhood signals
- `Orient`: fit against user profile
- `Decide`: keep, inspect, reject, or wait
- `Act`: next step

The qualitative assessment may use 1minAI only through the shared EA 1min
Manager. It may explain the deterministic score and propose at most two lazy
research actions; it must never change the score or turn an unknown into a
fact. Google Maps research runs only in the durable worker through a dedicated
`propertyquarry-operator` BrowserAct binding. A distance becomes evidence only
when the result is bound to exact listing coordinates, a validated Google Maps
URL, an identifier embedded in that URL, destination coordinates, a signed
attestation, and a bounded freshness window. Provider failure or incomplete
output leaves the fact unknown.

## 5. Data Contract

The workbench should receive one normalized payload from the backend.

Target shape:

```json
{
  "run": {
    "run_id": "string",
    "status": "processed",
    "progress": 100,
    "message": "string",
    "started_at": "iso",
    "completed_at": "iso"
  },
  "brief": {
    "country_code": "AT",
    "region_code": "vienna",
    "full_region_scope": true,
    "listing_mode": "buy",
    "selected_platforms": ["willhaben", "genossenschaften_at"],
    "priorities": ["lift", "family", "u-bahn"]
  },
  "results": [
    {
      "candidate_ref": "string",
      "rank": 1,
      "title": "string",
      "source_label": "string",
      "location_label": "string",
      "price_display": "EUR 659,000",
      "price_per_sqm_display": "EUR 7,448/m2",
      "area_display": "88.48 m2",
      "rooms_display": "3",
      "fit_score": 92,
      "tour": {
        "status": "ready",
        "url": "https://propertyquarry.com/tours/...",
        "eta_label": ""
      },
      "ooda": {
        "summary": "Lift, school, transit and price fit.",
        "rows": []
      },
      "risk": {
        "level": "medium",
        "summary": "Heating details missing."
      },
      "packet_url": "/app/research/..."
    }
  ],
  "selected_candidate_ref": "string"
}
```

The current payload can be adapted in `landing_view_models.py`, but the template should not keep rediscovering facts from nested legacy structures.

## 6. Component Inventory

Build a small product-specific component system inside the current templates before introducing a JS framework.

Required primitives:

- `AppShell`
- `RunStatusBar`
- `SearchBriefDrawer`
- `ResultsTable`
- `ResultRow`
- `TourPreview`
- `DossierPane`
- `OodaBlock`
- `RiskBlock`
- `InvestmentBlock`
- `ComparisonTable`
- `EvidenceList`
- `FeedbackControls`
- `PlanBadge`
- `EmptyState`
- `ErrorState`
- `LoadingState`

Implementation can start as Jinja macros or template sections. The important point is consistency and testability.

## 7. Implementation Plan

### Phase 0: Freeze Backend Behavior

Do not change provider crawling, investment research, billing, or notifications during the first UI rewrite.

Required before UI work:

- property release gates green
- one live processed run available
- one fixture with 360 ready
- one fixture with 360 queued
- one fixture with no results

### Phase 1: Add Workbench Payload

Files:

- `ea/app/api/routes/landing_view_models.py`
- `tests/test_propertyquarry_workspace_redesign.py`

Work:

- add a normalized `decision_workbench` payload
- derive `results`
- derive `selected_candidate`
- derive `tour.status`
- derive compact OODA rows
- derive risk severity
- derive table-ready investment values

Acceptance:

- no template logic has to inspect deep legacy candidate structures
- tests can assert normalized result fields directly

### Phase 2: Build Workbench Template

Files:

- `ea/app/templates/app/property_decision_workbench.html`
- `ea/app/templates/base_console.html` or a new `base_property_app.html`

Work:

- render three-zone desktop layout
- render selected property dossier on the right
- render 360 region first
- render mobile mode switch
- retain existing `/app/properties` route

Acceptance:

- finished run defaults to table + selected property
- active run defaults to status + compact table/placeholder
- search brief is drawer/collapsed by default when a run exists

### Phase 3: Add Selection Behavior

Files:

- `property_decision_workbench.html`
- optional `static/propertyquarry/workbench.js` if static assets are already supported

Work:

- embed normalized candidates as JSON
- row click updates selected candidate
- selected candidate is reflected in URL query param
- feedback saves without losing selected state
- browser back/forward works for selected candidate

Acceptance:

- no full-page transition is required to inspect top 5
- mobile can open and close the property pane

### Phase 4: Replace Old Default

Files:

- `ea/app/api/routes/landing.py`
- `landing_view_models.py`

Work:

- route `/app/properties` to the workbench template
- keep old template behind a temporary fallback flag
- remove fallback only after visual gates pass

Suggested flag:

- `PROPERTYQUARRY_WORKBENCH_FALLBACK=1`

Acceptance:

- default live app uses the workbench
- old routed pages remain deep-link compatible until removed

### Phase 5: Collapse Secondary Routes

Work:

- `/app/shortlist` redirects or renders the workbench with results focus
- `/app/research` redirects or renders workbench with dossier focus
- `/app/profile` opens brief/profile drawer
- `/app/alerts` opens notification/status drawer

Acceptance:

- primary user journey feels like one app
- deep links still work
- no dead duplicate pages

## 8. Audit Method

Every redesign iteration should be audited with screenshots and a scorecard.

### 8.1 Screenshot Set

Capture:

- desktop empty state
- desktop active run
- desktop finished results with 360 ready
- desktop finished results with 360 queued
- desktop property dossier
- desktop investment dossier
- mobile results
- mobile selected property
- mobile search brief drawer

### 8.2 Scorecard

Score each screen from 0 to 3.

- **0:** broken or misleading
- **1:** works but feels rough
- **2:** acceptable paid-product quality
- **3:** flagship quality

Categories:

- hierarchy
- decision speed
- visual noise
- table usability
- 360 prominence
- OODA concreteness
- trust/provenance
- mobile usability
- action clarity
- loading/empty/error state
- no inherited EA language

Release requirement:

- no category below `2`
- 360 prominence must be `3`
- action clarity must be `3`
- no inherited EA language must be `3`

## 9. Hard Gates

Add or extend gates so the redesign cannot regress.

Required DOM gates:

- PropertyQuarry app shell exists
- finished run shows results table
- finished run hides search form by default
- active run shows live run status
- active run hides full search form
- dossier shows 360 region before OODA
- dossier shows OODA before evidence/provenance
- every hero tile or glass card with hover styling has an `href` or button behavior
- no visible `Executive Assistant`, `Morning Memo`, `Office sync`, or `MyExternalBrain` product copy

Required browser gates:

- desktop row click updates dossier
- mobile result opens property pane
- mobile pane close returns to results
- search brief opens from drawer button
- 360 queued state is visible and does not look broken
- keyboard tab order reaches table, selected dossier action, and feedback controls

Required visual gates:

- screenshot baseline for desktop finished results
- screenshot baseline for desktop dossier with 360
- screenshot baseline for mobile result selection
- max allowed diff threshold documented in the test

## 10. Accessibility

Minimum bar:

- table rows are keyboard selectable
- selected row has `aria-selected`
- dossier pane has a heading and landmark
- drawer has focus trap when open
- status changes use a polite live region
- buttons have real labels
- color is never the only status indicator
- text fits on mobile

## 11. Mobile Rules

Mobile must not be a squeezed desktop.

Rules:

- default view: results list
- selected property opens as panel
- 360 stays first in selected panel
- brief is hidden behind a clear button
- sticky bottom action bar has at most three actions
- table columns collapse into row facts
- no horizontal scrolling for primary review

## 12. Migration Risk

Main risks:

- rebuilding too much backend while redesigning UI
- letting old routes remain as competing products
- adding decorative UI that worsens decision speed
- moving to a frontend framework before the product interaction model is proven
- screenshot tests becoming brittle without clear thresholds

Mitigation:

- freeze backend behavior for the first UI pass
- define one normalized workbench payload
- test the interaction model before broad route cleanup
- keep visual system restrained and table-first

## 13. Definition of Done

The redesign is done when:

- `/app/properties` feels like one focused property workbench
- finished results are table-first
- selected property is visible without route-hopping
- 360 is always the first dossier region
- OODA is concrete and above the fold
- investment research is visible for buy candidates
- search brief is available but not dominant after a run
- all clickable-looking UI is actually clickable
- mobile is designed as its own flow
- release gates include DOM, browser, and visual checks
- no inherited assistant copy is visible on PropertyQuarry

## 14. First Engineering Slice

The first implementation slice should be narrow but decisive:

1. add normalized `decision_workbench` payload
2. create `property_decision_workbench.html`
3. render finished run as table plus right dossier
4. make row selection update dossier client-side
5. add desktop and mobile browser tests
6. add screenshot baselines
7. route `/app/properties` to the new template behind a flag

Do not start with settings, billing, or more provider work. The product becomes premium when the core decision loop feels premium.
