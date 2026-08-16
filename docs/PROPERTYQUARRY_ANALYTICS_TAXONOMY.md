# PropertyQuarry Analytics Taxonomy

Rybbit and any future analytics provider are public-safe measurement lanes. Analytics must improve the product loop without carrying exact private property payloads, raw notes, personal identifiers, or signed URLs.

## Runtime Scope

Rybbit is disabled by default and enabled only through explicit environment flags plus a site ID. The default runtime scope is public marketing, pricing, registration, sign-in, editorial, and conversion surfaces. Authenticated app routes, API routes, workspace-access links, generated tours, research URLs, and listing URLs are skipped or masked by default.

Authenticated app analytics require `PROPERTYQUARRY_RYBBIT_AUTHENTICATED_ENABLED=1` and still must not call `identify`, send principal IDs, send emails, or attach raw property/search payloads. App-level analytics, when explicitly enabled, are limited to aggregate route health, conversion state, device class, latency buckets, and coarse UI error buckets.

## Event Names

```text
pq.search.started
pq.search.results_viewed
pq.search.agent_created
pq.search.agent_updated
pq.search.agent_notification_sent
pq.search.suppressed_viewed
pq.property.opened
pq.property.map_opened
pq.dossier.opened
pq.tour.opened
pq.flythrough.opened
pq.decision.saved
pq.reason.selected
pq.agent_question.created
pq.document.requested
pq.packet.saved
pq.email.clicked
```

## Allowed Properties

```text
country_code
region_code
listing_mode
property_type
decision_mode
fit_bucket
provider_key
provider_family
provider_quality_bucket
surface
cta_key
route_family
device_class
latency_bucket
error_bucket
```

## Forbidden Properties

```text
exact address
property URL
signed link token
email
phone
principal ID
Telegram chat ID
run ID
listing ID
raw household note
raw Dadan transcript
document text
```
