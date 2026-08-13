# Pricing

## Commercial model

Freemium with paid research depth.

## Proposed tiers

### Free

- account creation
- preference profile
- limited platform search
- all available matches shown in ranked order, with an honest total
- concise decision summary
- no advanced research reruns

### Plus

Indicative price: 3 EUR per month

- broader search coverage
- ongoing saved search alerts
- more visible fit reasoning
- richer shortlist review pages
- moderate research depth

### Agent

Indicative price: 79 to 149 EUR per month

- deep research on shortlisted listings
- agent-triggered follow-up investigation
- better enrichment of missing listing facts
- premium property pages
- priority search and refresh

## Paywall logic

The paid boundary should not only be “more hits”.

No tier silently hides ranked homes by score. Free may limit provider breadth,
reruns, alerts, and research depth while still showing the available ranked
result set and its total.

It should also control:

- research depth
- rerun frequency
- alert volume
- number of active saved searches
- detail-enrichment triggers
- agent-assisted follow-up

## Billing note

PayFunnels is the preferred first commercial lane for self-serve plan gating and upgrades.

The first enabled paid lane is `Plus` at `3 EUR / 30 days`.

PropertyQuarry now supports:

- PayFunnels-hosted checkout as the preferred first checkout lane for paid plans
- a signed PayFunnels webhook for fail-closed plan activation
- PayPal as the direct fallback lane where PayFunnels is not configured

Checkout remains unavailable until the selected provider has dedicated Live
credentials and a fresh same-principal safe-handoff canary for Plus and Agent.
Brilliant Directories is not a billing or entitlement authority.
