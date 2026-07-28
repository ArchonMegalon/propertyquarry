# PropertyQuarry SLO Release Evidence

PropertyQuarry launch authority requires a fresh metrics snapshot captured from
the authenticated private `/internal/metrics` route and an offline flagship SLO
receipt for the exact release. The evidence expires after 15 minutes. It is
private operator material and must not be committed, uploaded as a public CI
artifact, or pasted into tickets or chat.

## Capture contract

Run the capture from the release host against a localhost or private-IP target.
The tool deliberately refuses public hostnames, URL credentials, paths, query
strings, and fragments so the bearer token cannot be sent to an arbitrary
endpoint. It reads the token from `EA_API_TOKEN`; never put the token on the
command line.

```bash
umask 077
export PROPERTYQUARRY_RELEASE_COMMIT_SHA='<full-40-character-release-sha>'
export PROPERTYQUARRY_RELEASE_IMAGE_DIGEST='sha256:<64-hex-image-digest>'
export PROPERTYQUARRY_EXPECTED_API_REPLICAS=1

python3 scripts/propertyquarry_slo_capture.py \
  --base-url 'http://127.0.0.1:8090' \
  --host-header 'propertyquarry.com' \
  --release-sha "${PROPERTYQUARRY_RELEASE_COMMIT_SHA}" \
  --image-digest "${PROPERTYQUARRY_RELEASE_IMAGE_DIGEST}" \
  --replica-id "$(docker inspect --format '{{.Id}}' propertyquarry-api)" \
  --replica-count "${PROPERTYQUARRY_EXPECTED_API_REPLICAS}" \
  --metrics-snapshot '_completion/propertyquarry_slo_evidence/metrics-current.prom' \
  --metrics-probe '_completion/propertyquarry_slo_evidence/metrics-probe-current.json'
```

The response must be Prometheus text with `Cache-Control: no-store`. The tool
writes both artifacts atomically with mode `0600`. The probe records the
release SHA, immutable image digest, replica identity and count, metrics hash,
private/authenticated route result, and `credential_persisted: false`. It does
not record the token or target URL.

Operator environment controls are deliberately narrow:

- `PROPERTYQUARRY_EXPECTED_API_REPLICAS` binds the expected positive replica count.
- `PROPERTYQUARRY_SLO_CAPTURE_PRINCIPAL_ID` selects the dedicated metrics principal.
- `PROPERTYQUARRY_SLO_CAPTURE_TIMEOUT_SECONDS` bounds the private request.
- `PROPERTYQUARRY_REQUIRE_SLO_RELEASE_EVIDENCE=1` applies the production gate to an isolated non-production candidate; production cannot bypass it.
- `PROPERTYQUARRY_SLO_METRICS_SNAPSHOT`, `PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT`, and `PROPERTYQUARRY_SLO_EVIDENCE_RECEIPT` connect operator-private artifacts to the focused release bundle.
- `PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT`,
  `PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT`,
  `PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE`,
  `PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT`,
  `PROPERTYQUARRY_DASHBOARD_RENDER_RECEIPT`,
  `PROPERTYQUARRY_STRUCTURED_LOG_QUERY_RECEIPT`, and
  `PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT` bind every raw deployed-
  observability input consumed by the canonical bundle verifier.

## Offline flagship gate

Run the validator immediately after capture. It contacts no live monitoring
service and uses the pinned, preinstalled `promtool` and `amtool` binaries.

```bash
python3 scripts/propertyquarry_slo_evidence.py \
  --flagship \
  --release-sha "${PROPERTYQUARRY_RELEASE_COMMIT_SHA}" \
  --image-digest "${PROPERTYQUARRY_RELEASE_IMAGE_DIGEST}" \
  --metrics-snapshot '_completion/propertyquarry_slo_evidence/metrics-current.prom' \
  --metrics-probe '_completion/propertyquarry_slo_evidence/metrics-probe-current.json' \
  --prometheus-range "${PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE}" \
  --prometheus-range-receipt "${PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT}" \
  --receipt '_completion/propertyquarry_slo_evidence/release-gate.json' \
  --overwrite-receipt
```

For the focused release bundle, export the SLO inputs and every deployed-
observability input. The bundle reruns both validators fail closed and passes
their resulting receipts and raw inputs into the consolidated gold-status
gate.

```bash
export PROPERTYQUARRY_SLO_METRICS_SNAPSHOT='_completion/propertyquarry_slo_evidence/metrics-current.prom'
export PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT='_completion/propertyquarry_slo_evidence/metrics-probe-current.json'
./scripts/property_release_gates.sh
```

## Production deploy boundary

`scripts/deploy_propertyquarry.sh` is an unprivileged handoff client, not a
deployment implementation. It opens the fixed release-controller binary,
manifest, digest pin, signed request, and candidate root once; verifies their
stable file identities; then replaces itself with the controller through the
already-open controller FD. It never imports checkout Python, reads candidate
receipts, opens Docker/PostgreSQL, or starts/stops/switches traffic.

The installed controller owns the lock, incomplete-run containment and
reconciliation, canonical Compose plan, migrations, SLO/monitoring evidence,
Gold decision, the immutable `propertyquarry-cloudflared` image/config, and promotion. Its normal run
contains ingress and every writer before validating the checkout. The
checkout's `--preflight-only` handoff selects a distinct read-only controller
operation that forbids containment and state mutation and returns an explicit
disposition.

Promotion requires a receipt signed by an independent release-control
authority. This repository intentionally has no sign/issue mode. The v2
receipt binds the exact release SHA and digest, deployment ID, target ID,
Compose project, public origin, API/worker/scheduler/render/ingress containers,
actor, the pinned writer-topology SHA-256, UTC issue/expiry times, action, key
ID, and a 32-hex nonce. Its lifetime is at most five minutes. The deploy actor
may configure only the receipt, target, and actor bindings:

```bash
export PROPERTYQUARRY_DEPLOY_DRAIN_RECEIPT='/secure/release-control/propertyquarry-drain.json'
export PROPERTYQUARRY_DEPLOY_PROMOTION_RECEIPT='/secure/release-control/propertyquarry-promotion.json'
export PROPERTYQUARRY_DEPLOY_TARGET_ID='propertyquarry-prod-primary'
export PROPERTYQUARRY_DEPLOY_ACTOR_ID='<stable-operator-or-service-id>'
```

The candidate checkout is never a release authority. Production privileged
operations go only through the independently installed controller at
`/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller`.
Its reviewed manifest is fixed at
`/etc/propertyquarry/release-control/external-deploy-controller.v1.json`, and
its independent one-line digest pin is fixed at
`/etc/propertyquarry/release-control/external-deploy-controller.sha256`.
Controller, manifest, and pin must be root-owned, single-link, non-symlink
files with exact modes. The wrapper hashes the opened controller FD against
the opened pin FD; the controller then validates the manifest and its own FD
again. Environment and CLI path/hash selectors are ignored or rejected. The
checked-in controller manifest and digest-pin template are deliberately
`UNCONFIGURED`, so a checkout that replaces its local verifier with `exit 0`
still cannot authorize migration, recovery, rollback, or traffic.

Until release control installs and independently attests that native
controller, repository tests prove only that the wrapper fails closed and
hands an opened FD to a test recorder. They do **not** prove real containment,
database fencing, evidence verification, receipt consumption, or traffic
promotion semantics. Those remain operational launch blockers.

`PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST` names an invoking-user-owned,
single-link, mode-`0400` transport file. Root ownership would make it unreadable
to the required unprivileged wrapper and is not a trust mechanism. The wrapper
only pins the exact bytes in an FD; the installed controller alone validates
the signature, challenge, nonce, freshness, target, and monotonic authority.
Privileged automation must invoke the installed native controller directly
and must never run this checkout wrapper as root.

The fixed controller lock is acquired before candidate evidence is examined.
Ingress, API, dedicated worker, scheduler, render, and the pinned migrator are
contained first on every production invocation. Consequently an expired new
receipt or a missing, deleted, corrupt, or rolled-back local journal cannot
prevent crash recovery. All production journal mutations and drain consumption
are controller operations under that inherited lock.

Release control provisions the v2 external drain keyring at
`/etc/propertyquarry/release-control/deploy-drain-keyring.v2.json`. Rotation has
strict epochs, activation instants, an explicit old/new overlap cutoff, and a
revocation cutoff. The external monotonic authority rejects an earlier epoch;
private signing material never enters the checkout or deploy environment.

Local journal and consumption-ledger JSON are caches, not replay authority.
The installed controller performs signed append-only compare-and-swap against
an independent monotonic backend. Every seal binds target database identity,
generation, previous seal hash, local state hash, phase, release/image,
inventory, challenge, migration plan, and migration result. Deleting or
restoring an older valid cache cannot lower the external generation. If the
authority is unavailable or a cache does not match it exactly, production
remains contained and fails closed.

Database identity comes from PostgreSQL, never from a literal DSN: cluster
system identifier, database OID/name, a durable target UUID, and primary state.
Flagship production requires a dedicated database plus separate external
control, object-owner, migrator, and per-epoch runtime roles. Runtime roles are
non-superusers, own no objects, cannot reach owner/control roles, and become
`NOLOGIN` during maintenance. `CONNECT` is permanently revoked from `PUBLIC`.
The controller terminates and repeatedly proves zero target backends, rejects
prepared transactions and unapproved logical/background writers, and records a
durable fence before DDL. The migrator verifies that fence and commits ordered
DDL, migration ledger, challenge/plan digest, and migration-result digest in
one transaction. Advisory locks are serialization only, never the fence.

The reviewed provisioning contract is
`config/release/propertyquarry_database_fence_policy.v1.json`; release control
installs its active counterpart at
`/etc/propertyquarry/release-control/database-fence-policy.v1.json`. The tracked
policy is deliberately `UNCONFIGURED`. The current general-purpose Compose
defaults use the `postgres` database/shared superuser and automatic restarts,
so they are not a flagship database authority: the installed controller must
reject them until dedicated roles, secrets, database identity, and controller-
owned runtime start are provisioned.

The pre-migration authorization at `PROPERTYQUARRY_DEPLOY_DRAIN_RECEIPT` binds the server-derived database identity,
actual role/session inventory, topology digest, drain challenge, exact
migration plan, release/image, nonce, and TTL. After migration, promotion
authorization at `PROPERTYQUARRY_DEPLOY_PROMOTION_RECEIPT` is a distinct,
short-lived signature issued only after it can bind the maintenance-
authorization digest, externally sealed migration-result digest, and exact
candidate Gold evidence. Changing `DATABASE_URL`, target identity, plan,
result, or evidence breaks the chain. Only the controller may activate the new
runtime-role epoch and consume promotion authority immediately before ingress.

Any pre-commit failure leaves ingress stopped and restores only writers that
were previously active. Any committed-migration, SLO, monitoring, range,
alert-delivery, gold, receipt-replay, tunnel, or public-version failure leaves
ingress and candidate API/worker/scheduler/render stopped. A consumed receipt
is single-use even when promotion subsequently fails; request a new signed
receipt after resolving the failure.

The release-controlled containment topology is pinned at
`config/release/propertyquarry_deploy_writer_topology.v1.json` and its digest is
signed into authorization. Local Docker inspection stops the exact pinned
containers, including a crashed migrator, but is diagnostic rather than a
database identity boundary. The server-side role/session fence is authoritative.

Recovery and rollback execute only as signed operations inside the same
installed controller. They use the same lock, external journal, database fence,
and forward-only schema epoch. Caller-selected traffic commands and a
confirmation phrase alone cannot migrate or switch traffic.

Operationally, the repository remains fail closed until the external
controller, controller manifest and digest pin, canonical Compose plan, drain
keyring, monitoring topology/tool pins, monotonic CAS backend, role-separated
dedicated database, immutable Cloudflared digest/config binding, secrets, and
initial sealed genesis are provisioned. Source-only local files cannot solve
privileged deployment or rollback.

## Canonical deployed-observability bundle

Flagship promotion additionally consumes seven private raw observability
artifacts, alongside the SLO metrics snapshot and probe:

- monitoring runtime receipt
  (`propertyquarry.monitoring-runtime-proof.v1`)
- isolated alert-delivery receipt
  (`propertyquarry.alert-delivery-receipt.v1`)
- 30-day Prometheus range receipt
  (`propertyquarry.prometheus-range-receipt.v1`)
- the exact raw Prometheus `query_range` JSON bound by that range receipt
- dashboard-render receipt
- structured-log-query receipt
- distributed-trace-query receipt

Do not accept the producers' status booleans or stored hashes directly. The
canonical verifier re-reads every input, recomputes canonical payload hashes,
recomputes the raw range byte/hash and normalized matrix identity, and checks
release, Prometheus-config, replica-set, and alert-receipt links:

```bash
python3 scripts/propertyquarry_observability_receipts.py verify \
  --release-sha "${PROPERTYQUARRY_RELEASE_COMMIT_SHA}" \
  --image-digest "${PROPERTYQUARRY_RELEASE_IMAGE_DIGEST}" \
  --monitoring-receipt '_completion/propertyquarry_monitoring/runtime-proof.json' \
  --prometheus-range-receipt '_completion/propertyquarry_monitoring/range-receipt.json' \
  --prometheus-range-response '_completion/propertyquarry_monitoring/range-response.json' \
  --alert-delivery-receipt '_completion/propertyquarry_monitoring/alert-delivery.json' \
  --dashboard-render-receipt '_completion/propertyquarry_monitoring/dashboard-render.json' \
  --structured-log-query-receipt '_completion/propertyquarry_monitoring/structured-log-query.json' \
  --distributed-trace-query-receipt '_completion/propertyquarry_monitoring/distributed-trace-query.json' \
  --metrics-snapshot '_completion/propertyquarry_slo_evidence/metrics-current.prom' \
  --metrics-probe '_completion/propertyquarry_slo_evidence/metrics-probe-current.json' \
  --output '_completion/propertyquarry_monitoring/verification.json'
```

The verifier exits `2` on any schema, duplicate-key, non-finite value,
canonical hash, raw byte count, expected-replica, release identity, target
freshness, or cross-receipt mismatch. It refuses to replace an existing output
unless `--overwrite` is explicit and always writes mode `0600`.

Set `PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT`,
`PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT`,
`PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE`, and
`PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT`,
`PROPERTYQUARRY_DASHBOARD_RENDER_RECEIPT`,
`PROPERTYQUARRY_STRUCTURED_LOG_QUERY_RECEIPT`, and
`PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT` to these private raw artifacts
for the deploy. Gold uses `--require-launch-evidence`; a previously generated
green receipt without its raw inputs has no promotion authority.
