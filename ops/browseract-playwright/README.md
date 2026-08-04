# PropertyQuarry BrowserAct operator

This image is the reviewed account-side operator for the optional Google Maps
missing-fact lane. It clones, inspects, configures, and publishes the licensed
BrowserAct workflow without putting BrowserAct credentials or session state in
the PropertyQuarry runtime image.

The build is reproducible:

- Microsoft Playwright is pinned by tag and Linux/amd64 manifest digest.
- Playwright is exact-version locked in `package-lock.json`.
- dependency audit and operator unit tests run during every image build.
- the runtime user is unprivileged and the operator writes receipts and
  screenshots with owner-only permissions.

Build:

```sh
docker build --tag propertyquarry-browseract-operator:1.0.0 ops/browseract-playwright
```

Run with a private profile/evidence directory, a read-only env-file mount, a
read-only root filesystem, dropped capabilities, and the host operator UID/GID.
The supported modes are `inspect`, `clone`, `publish`, `configure-extract`, and
`configure-loop`. Configuration modes require the exact reviewed step name and
fail closed if the live editor no longer matches the template contract.

The live PropertyQuarry binding remains separate from this browser profile. It
must be owned by the `propertyquarry-operator` principal, scoped only to
`google_maps_distance_research`, and map that service to the published workflow
ID. API and scheduler containers must not receive the BrowserAct API key or the
binding authority.
