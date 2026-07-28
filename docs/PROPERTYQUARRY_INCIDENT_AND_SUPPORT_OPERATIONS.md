# PropertyQuarry Incident and Support Operations

This runbook implements the policy contract in
`config/monitoring/propertyquarry_incident_support.v1.json`. It defines required
behavior; it does not prove staffing, endpoint readiness, drill completion, or
live response times. Flagship readiness requires fresh independent evidence for
those facts.

## Activation and ownership

Open the incident through the governed paging endpoint and assign an incident
commander, operations lead, communications lead, customer-support lead, and
security/privacy lead. Record the exact release identity, start time, affected
markets, customer impact, current severity, and decision timeline. Never put
credentials or private customer payloads in the incident record.

## Severity and acknowledgement

Classify against the canonical SEV0–SEV3 definitions and response clocks in the
policy artifact. Use the highest plausible severity until impact and integrity
are understood. Escalate immediately for active compromise, destructive loss,
corruption, broad outage, or a potentially reportable privacy event. Preserve
the alert and metric snapshot before changing state.

## Customer status and support

Publish bounded, factual status updates through the governed status-page
workflow at the required severity cadence. Link support cases by opaque
correlation ID, not copied customer data. Confirm the support system enforces
least privilege and retention, and route market-language responses to qualified
staff. A draft, test notification, or unavailable endpoint is not proof of
customer communication.

## Security and privacy

The security/privacy lead starts the internal breach assessment and preserves
release identity, bounded logs, access/audit events, provider receipts, and the
decision timeline. Apply the regulatory notification rule only after the
controller assessment required by applicable law. Do not delete primary
evidence, paste secrets into case notes, or disable a privacy/security control
to restore service.

## Recovery coordination

Use the PropertyQuarry rollback and PostgreSQL disaster-recovery runbooks for
their governed operations. Keep one incident commander responsible for the
decision, exact target release, verification commands, and customer
communication. Do not perform an unrecorded restart, destructive cleanup, or
manual data repair.

## Closure

Close only after customer impact and the timeline are recorded, alerts and
service health are restored, the release identity is reverified, required
customer or regulatory follow-up is complete, and every follow-up has an owner
and due date. SEV0, SEV1, and SEV2 incidents require a postmortem within the
policy deadline.
