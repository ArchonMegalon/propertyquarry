# Repository Isolation

PropertyQuarry is the standalone product runtime. The active release surface is intentionally narrow:

- `ea/` application runtime used by the property API and scheduler
- `docker-compose.property.yml`
- `docker-compose.property-legacy-edge.yml` only when an intentional legacy edge alias is still required
- `ea/Dockerfile.property`
- `config/`, `docs/`, `scripts/`, and `tests/` that are property-facing
- `.github/workflows/smoke-runtime.yml`

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

The only publishable runtime packages are
`ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime` and
`ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime`. Their first
publication must originate from this repository. Packages linked to the former
combined repository are outside the standalone release plane even when their
names contain `propertyquarry`.

Run the offline repository-identity gate after fetching the canonical repository
through both compatibility-named local refs:

```text
python3 scripts/check_property_mirror_role.py \
  --canonical-ref refs/remotes/origin/main \
  --mirror-ref refs/remotes/propertyquarry/main \
  --write _completion/propertyquarry_mirror_role/receipt.json
```

The compatibility-named gate never fetches, and both URL constants resolve to
`https://github.com/ArchonMegalon/propertyquarry.git`. It proves exact commit
identity and clean local state without granting authority to another repository.
Its receipt is deliberately scoped to local Git config, objects, and refs and
always reports `network_freshness_proven: false`. The corresponding CI lane
rejects Git URL rewrites, fetches standalone PropertyQuarry `main`, runs the
offline gate in a single-worktree checkout, and preserves the receipt. A
malformed manifest, wrong remote, missing history, dirty worktree, or unexpected
extra worktree blocks the ordinary-CI aggregate and therefore blocks release.

A pull request in `ArchonMegalon/propertyquarry` is review evidence only. Push
and workflow-dispatch release events require exact standalone `main` identity;
no commit from another repository can satisfy the gate. Fetch remains an
explicit operator step, so the offline receipt never claims lasting network
freshness.

## Release-control v2 authority status

The checked-in v2 supervisor is non-authoritative and inert. Its
`release-preflight` and `release-run` entrypoints consume and dispose of the
bounded bearer channel, perform no release effect, and return the protocol
failure class. The workflow lane is therefore a fail-closed integration
contract, not production launch evidence. A requested legacy activation is
rejected explicitly, and an always-running requested-action result job prevents
skipped security, activation, or launch work from producing a green requested
release run. Production authority remains blocked until a separately installed,
authenticated supervisor implements and proves the complete live-evidence,
activation, rollback, and lifecycle contract.
