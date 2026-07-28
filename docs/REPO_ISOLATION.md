# Repository Isolation

PropertyQuarry is the standalone product runtime. The active release surface is intentionally narrow:

- `ea/` application runtime used by the property API and scheduler
- `docker-compose.property.yml`
- `docker-compose.cloudflared.yml`
- `docker-compose.property-legacy-edge.yml` only when an intentional legacy edge alias is still required
- `ea/Dockerfile.property`
- `config/`, `docs/`, `scripts/`, and `tests/` that are property-facing
- `.github/NO_GITHUB_ACTIONS.md`

The extraction still carries inherited archives from the broader EA and Chummer work. These are not part of the PropertyQuarry runtime path and must not be referenced by the property compose file, hardened Dockerfile, or property release gates:

- `.codex-design/`
- `.codex-studio/`
- `feedback/`
- `skills/`
- `docs/black_ledger_newsroom/`
- `docs/chummer5a_parity_lab/`
- `docs/chummer_explain_narration_packs/`
- `docs/chummer_governor_packets/`
- `docs/chummer_launch_followthrough/`
- `docs/chummer_operator_safe_packets/`
- `docs/chummer_organizer_packets/`
- `scripts/bootstrap_chummer6_guide_skill.py`

If one of these directories is needed for future work, move that work to the owning repository first or add a dedicated migration issue. Do not wire it into the PropertyQuarry deploy path.

Host-level recovery scripts are quarantined operator artifacts. `scripts/harden_propertyquarry_docker.sh` and `scripts/recover_host_after_reboot.sh` must stay explicitly guarded behind `PROPERTYQUARRY_HOST_RECOVERY_ALLOW=1`, support dry runs, and must not be treated as normal release/runtime entrypoints.

Use `python3 scripts/check_property_repo_isolation.py` for a local check. Before
a public deploy, run the authenticated bundle with
`./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py property-release-gates`.

## Canonical repository authority

`ArchonMegalon/propertyquarry` is the sole canonical source and release-authority
repository. PropertyQuarry does not accept release identity, commits, manifests,
container provenance, runtime configuration, or deploy authority from
MyExternalBrain or `ArchonMegalon/property`.

The authoritative runtime packages are immutable local image IDs built as
`propertyquarry-standalone-web-runtime` and
`propertyquarry-standalone-render-runtime`. A registry copy may be used for
backup or transfer, but a registry and GitHub are not release authority.

Run the offline repository-role gate in the canonical checkout:

```text
python3 scripts/check_property_repository_role.py \
  --expected-repository ArchonMegalon/propertyquarry \
  --expected-role canonical \
  --require-clean-worktree \
  --write _completion/propertyquarry_repository_role/receipt.json
```

The gate reads
`config/release/propertyquarry_repository_role.v1.json`, whose canonical and
legacy repositories must be distinct. It proves that this checkout's exact
origin, manifest authority, absence of GitHub workflows, local Docker authority
surfaces, and expected role agree with
that shared policy. Its receipt is deliberately scoped to local policy, Git
config, checkout, and Docker release surfaces and always reports
`network_freshness_proven: false`. A malformed policy, self-reference, wrong
remote, noncanonical manifest, dirty worktree, GitHub Actions workflow, or
missing local authority surface blocks deployment.

Pushing source does not deploy it. `scripts/deploy_propertyquarry.sh` builds and
deploys on the local Docker host, and
`scripts/propertyquarry_local_deployment_receipt.py` binds exact images,
Compose files, services, migration, health, security posture, and localhost
readiness to the canonical runtime commit.

`ArchonMegalon/property` is a legacy, noncanonical verifier-only repository. It
may point operators to this repository, but it must contain no canonical
release-manifest markers, workflow, image publication, deploy, or runtime
authority. Different legacy and canonical heads are expected; the legacy head
is never a second release candidate.

## Local Docker authority status

The authenticated local OCI controller package remains an additional
verification/control component. Application release authority is the complete
local Compose deployment receipt, not an absent external controller or remote
runner. Core production readiness requires the exact candidate proof plus a
passing current local Docker receipt. Advanced Visual stays additive and fails
closed until its provider receipts are bound.
