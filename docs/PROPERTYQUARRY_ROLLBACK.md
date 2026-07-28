# PropertyQuarry Rollback Runbook

Production rollback is an independently authorized release-control operation.
The checkout can validate immutable release identifiers and produce a dry-run
plan, but it cannot run schema checks, select traffic commands, migrate, or
switch traffic. Those actions belong to the root-owned controller at
`/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller`.

## 1. Identify immutable releases

Record the current and target releases from signed release receipts or the
live, verified `/version` response:

```bash
export PROPERTYQUARRY_ROLLBACK_CURRENT_RELEASE='<full-current-40-character-git-sha-or-image-digest>'
export PROPERTYQUARRY_ROLLBACK_PREVIOUS_RELEASE='<full-previous-40-character-git-sha-or-image-digest>'
```

Accepted values are a full 40-character Git SHA, `sha256:<64 hex>`, or an
immutable image reference ending in `@sha256:<64 hex>`. Mutable tags, branches,
short SHAs, and identical current/target identities are rejected.

Never use an old application image as authority to reverse database DDL. The
controller requires a forward-schema compatibility proof and forbids schema
epoch decrement. If the target cannot operate safely on the current schema,
rollback stays contained and a forward repair release is required.

## 2. Stage and prove the target privately

Stage the exact target under an isolated project, non-public host port, unique
container names, and Cloudflare disabled. Use a disposable restore of the
production backup for compatibility checks. Do not point the candidate at the
production database and do not run production migrations from the checkout.

Retain target health, version, authentication, durable-worker heartbeat,
scheduler, SLO, monitoring, and Gold evidence. Release control binds those
artifacts, the current/target
identities, server-derived database identity, current schema epoch, route,
nonce, and expiry into one signed rollback authorization.

## 3. Dry-run

Dry-run validates immutable identities and writes a command-free `0600` plan:

```bash
./scripts/propertyquarry_rollback.py \
  --receipt _completion/rollback/propertyquarry-rollback-dry-run.json
```

The plan names `external_controller.rollback-run`; it contains no caller-
selected schema, traffic, or verification command. Environment variables such
as `PROPERTYQUARRY_ROLLBACK_TRAFFIC_SWITCH_COMMAND` have no execution authority.

## 4. Obtain authorization and execute through release control

Release control supplies an absolute path to a short-lived signed authorization
outside the checkout:

```bash
/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller \
  rollback-run \
  --signed-authorization /run/propertyquarry/release-control/rollback-authorization.json
```

`./scripts/propertyquarry_rollback.py --execute` always fails closed
before parsing candidate command selectors or importing any candidate
controller helper. It is not an execution proxy. The independently installed
controller verifies the signature and complete
binding, acquires `/var/lock/propertyquarry/deploy-controller.lock`, reconciles
the external append-only journal, contains ingress and the pinned API, worker,
scheduler, and migrator writer set, reconciles the
durable database fence, proves forward-schema compatibility, performs the
traffic decision, verifies the target, and advances the external monotonic
seal. Controller rejection has no local fallback.

The checked-in controller manifest is deliberately `UNCONFIGURED`; production
execution is blocked until release control installs and pins the reviewed
controller, external manifest, drain keyring, database policy, and monotonic
authority.

## 5. Failure handling

Any missing, expired, mismatched, replayed, or rejected authorization leaves
ingress, API, dedicated worker, scheduler, and dependent render runtime in the
controller's fail-safe state. Preserve the local receipt and the controller's
signed journal/seal receipts. Never retry a traffic switch from a checkout
command and never lower the schema epoch.
