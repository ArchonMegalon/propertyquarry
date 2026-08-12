# PropertyQuarry launch room

PropertyQuarry has one release proof plane: the governed local Docker host.
GitHub stores source and accepts pushes, but GitHub Actions, hosted runners,
workflow dispatch, and Actions artifacts are not used.

The local operator entrypoint is:

```text
scripts/deploy_propertyquarry.sh --preflight-only
scripts/deploy_propertyquarry.sh
./scripts/propertyquarry_release_python.sh \
  scripts/propertyquarry_release_make_dispatch.py \
  verify-local-docker-deployment-authenticated
```

The authenticated local-deployment verifier invokes the launch room with
`--require-local-runtime-ready`. It fails closed unless the exact candidate
proof, exact-envelope mode-`0600` local Docker receipt, and clean worktree all
pass. This proves the local production runtime only. It does not grant public
launch authority. The source-only `release-preflight` is hermetic and never
sends mutation smoke requests to an ambient EA or PropertyQuarry runtime; live
API mutation smokes remain explicit runtime-operations commands.

Deployment builds immutable local web and render images, runs the migration,
starts the complete Compose topology, waits for health, probes localhost, and
writes the ignored mode-`0600` receipt:

```text
state/release/propertyquarry-local-deployment.v1.json
```

`scripts/propertyquarry_local_deployment_receipt.py` binds that receipt to:

- the canonical runtime commit and current release envelope;
- exact web and render image IDs;
- SHA-256 values for both Compose files;
- the `property` Compose project and exactly seven expected services;
- successful one-shot migration;
- healthy API, worker, scheduler, and Postgres containers;
- running render and Cloudflare tunnel containers;
- numeric non-root application users, no privileged containers, no Docker
  socket mounts, and `no-new-privileges`;
- the localhost `/health/ready` probe; and
- a live audit of the persisted public-tour volume proving raw `tour.json`
  manifests contain no private keys and every `tour.private.json` is mode
  `0600`; and
- `github_actions_used: false`.

No database URL, provider credential, OAuth secret, tunnel token, or reusable
environment value is serialized into the receipt.

Legacy tour-volume repair is a one-time, snapshot-first operation. Use the
exact runtime image and a new empty backup volume; the repair refuses to
mutate without that backup, atomically rebuilds public manifests through the
current narrow allowlist, retains removed values in private receipts, and
writes a count/digest-only receipt:

```text
docker volume create property_propertyquarry_public_tours_preprivacy_20260728
docker run --rm --entrypoint python \
  -v property_propertyquarry_public_tours:/data/public_property_tours \
  -v property_propertyquarry_public_tours_preprivacy_20260728:/backup \
  -v /docker/property/state/release:/receipts \
  PROPERTYQUARRY_EXACT_WEB_IMAGE \
  /app/scripts/propertyquarry_public_tour_volume_privacy.py \
  --root /data/public_property_tours \
  --apply \
  --backup-root /backup \
  --receipt /receipts/propertyquarry-public-tour-volume-privacy.v1.json
```

The backup volume is retained for rollback and is never mounted by the
application. Subsequent deployments run the same tool in audit mode through
the local deployment receipt and fail closed on drift.

The launch-room view combines this deployment receipt with the shared
repository-role policy, marked release manifest, exact candidate-bound browser
proof, and local Git state. It reports separate source, journey, and browser
counts, Core versus Advanced Visual posture, local deployment state, and the
next local operator action.

Core Gold may be local-runtime-ready when the exact candidate proof and local
Docker receipt both pass. Advanced Visual Gold remains additive and
`unavailable_unbound_producer_receipts` until its provider evidence is bound.
The running local Cloudflare container proves local tunnel process health; it
does not by itself claim fresh public-network reachability.

Public launch is a separate fail-closed authority boundary. An unsigned JSON
file inside the checkout is not authority and can never make the launch room
ready. The reserved external receipt location is
`/run/propertyquarry/release-control/propertyquarry-public-launch-authority.v1.json`;
`PROPERTYQUARRY_PUBLIC_LAUNCH_AUTHORITY_RECEIPT` may point at a future external
receipt, but the launch room deliberately ignores its claims until an
independently configured signature verifier and pinned keyring exist outside
the checkout. That verifier must bind the canonical repository, current
envelope, runtime commit, exact image digest, issuance/expiry, and replay-safe
receipt identity to three independently evidenced requirements:

- Google Play public launch and store-policy completion;
- safely configured paid billing with a proven no-second-login handoff; and
- encrypted off-host backup plus a successful restore drill.

`--require-production-ready` remains blocked with
`external_public_launch_authority_verifier_unconfigured` until that verifier is
implemented and its authenticated receipt passes. Local deployment health,
catalog entries, caller-selected files, or release-gate acceptance cannot
satisfy it.
