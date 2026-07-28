# PropertyQuarry Host Reboot Recovery

Host recovery is an independently authorized release-control operation. The
checkout validates the non-secret recovery identity and produces a dry-run
plan. It never receives the database owner/control/migrator credentials or the
Cloudflare tunnel token, and it never starts containers or runs migrations.
Those actions belong to the root-owned controller at
`/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller`.

## 1. Establish the immutable recovery identity

Use the exact release and dedicated route identities recorded by the last
externally sealed successful deployment:

```bash
export PROPERTYQUARRY_RELEASE_COMMIT_SHA='<full-40-character-release-sha>'
export PROPERTYQUARRY_COMPOSE_PROJECT_NAME='propertyquarry-production'
export PROPERTYQUARRY_RECOVERY_TUNNEL_ID='propertyquarry-production'
export PROPERTYQUARRY_RECOVERY_ROUTE_HOST='propertyquarry.com'
```

The tunnel identifier must be `propertyquarry-*`; the route must be
`propertyquarry.com` or one of its subdomains. Generic EA aliases, remote Docker
contexts, mutable release identities, and alternate Compose files are rejected.

Tunnel and database credentials remain in the installed controller's root-
owned secret store. Do not export `PROPERTYQUARRY_CF_TUNNEL_TOKEN`, database
owner/control credentials, or migrator credentials to this planner.

## 2. Run the command-free preflight

```bash
./scripts/propertyquarry_host_recovery.py \
  --receipt _completion/propertyquarry_host_recovery/preflight.json
```

The private `0600` receipt must report `status: dry_run`, dedicated
PropertyQuarry identities, the six steady-state services plus the ephemeral
migrator, `legacy_ea_aliases_allowed: false`, and
`credentials_owned_by_external_controller: true`. No Docker, HTTP, migration,
or controller command runs during dry-run.

## 3. Obtain authorization and execute through release control

Release control issues a short-lived signed recovery authorization bound to
the last sealed deployment, release/image, server-derived database identity,
schema epoch, route/tunnel identity, expected service topology, nonce, and
expiry. Supply it only to the installed-controller service or privileged
operator:

```bash
/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller \
  recovery-run \
  --signed-authorization /run/propertyquarry/release-control/recovery-authorization.json
```

`./scripts/propertyquarry_host_recovery.py --execute` always fails closed
before reading candidate Compose or importing any candidate controller helper.
It is not an execution proxy. The independently installed controller
authenticates the signed authorization, acquires the fixed deploy
lock, reads the external monotonic journal, and first contains ingress and all
known writers including any live migrator. It derives database identity from
PostgreSQL, restores or reconciles the durable role fence, rejects schema epoch
decrement, and starts only a release-authorized compatible runtime. Candidate
code cannot select Docker commands, credentials, migrations, or traffic.

The controller must prove the dedicated database and role policy, stable zero
writers before any migration, migration plan/result binding, private candidate
readiness, exact release version, fresh dedicated-worker heartbeat,
scheduler/render health, fresh Gold and observability evidence, and final
public version before releasing ingress.

## 4. Failure handling

Controller unavailability, an incomplete journal, stale authorization, missing
monotonic state, database mismatch, or failed proof leaves the runtime
contained. There is no local Compose fallback. Preserve the `0600` planner
receipt plus the controller's signed journal/seal receipts, correct the explicit
failure, and request a new authorization. Never use generic `docker compose
up`, `restart`, `down`, `--remove-orphans`, or a candidate migration command.

The checked-in controller manifest is deliberately `UNCONFIGURED`; recovery
cannot execute until the external controller, keyring, database policy, secret
store, and monotonic authority are provisioned.
