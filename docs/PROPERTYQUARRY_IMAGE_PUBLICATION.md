# PropertyQuarry local runtime images

PropertyQuarry does not use GitHub Actions or GHCR as release authority. The
governed local Docker daemon builds and runs both runtime images.

## Authority boundary

`scripts/deploy_propertyquarry.sh` requires a clean canonical checkout and
builds:

| Component | Compose service | Dockerfile |
| --- | --- | --- |
| Web | `propertyquarry-api` | `ea/Dockerfile.property-web` |
| Render | `propertyquarry-render-tools` | `ea/Dockerfile.property-render` |

The deployment resolves each build to its immutable local `sha256:<image-id>`
and passes those IDs—not mutable tags—to Compose. The web image ID is also
bound into the runtime release environment. Web, migration, worker, and
scheduler containers must use the web ID; the isolated render bridge must use
the distinct render ID.

Both images run as numeric UID/GID `10001:10001`. The web image uses the pinned
Distroless runtime and the render image uses the separately pinned offline
render package/wheel set. Neither receives a Docker socket.

## Receipt

After migration and health convergence,
`scripts/propertyquarry_local_deployment_receipt.py` writes:

```text
state/release/propertyquarry-local-deployment.v1.json
```

The mode-`0600` receipt binds:

- canonical runtime commit and release-envelope head;
- exact web and render image IDs;
- SHA-256 values for `docker-compose.property.yml` and
  `docker-compose.cloudflared.yml`;
- all seven local services and the `property` Compose project;
- migration exit `0`, required health states, and localhost readiness;
- non-root application users, no privileged containers, no Docker-socket
  mounts, and `no-new-privileges`; and
- `github_actions_used: false`.

It records no provider credential, OAuth secret, database URL, tunnel token, or
other reusable environment value.

A registry copy may be created for backup or host transfer, but it is not
required for deployment and cannot replace the exact local receipt.
