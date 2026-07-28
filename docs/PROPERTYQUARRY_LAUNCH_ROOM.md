# PropertyQuarry launch room

PropertyQuarry has one release proof plane: the governed local Docker host.
GitHub stores source and accepts pushes, but GitHub Actions, hosted runners,
workflow dispatch, and Actions artifacts are not used.

The local operator entrypoint is:

```text
scripts/deploy_propertyquarry.sh --preflight-only
scripts/deploy_propertyquarry.sh
python3 scripts/propertyquarry_launch_room.py
```

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
- `github_actions_used: false`.

No database URL, provider credential, OAuth secret, tunnel token, or reusable
environment value is serialized into the receipt.

The launch-room view combines this deployment receipt with the shared
repository-role policy, marked release manifest, exact candidate-bound browser
proof, and local Git state. It reports separate source, journey, and browser
counts, Core versus Advanced Visual posture, local deployment state, and the
next local operator action.

Core Gold may be production-ready when the exact candidate proof and local
Docker receipt both pass. Advanced Visual Gold remains additive and
`unavailable_unbound_producer_receipts` until its provider evidence is bound.
The running local Cloudflare container proves local tunnel process health; it
does not by itself claim fresh public-network reachability.
