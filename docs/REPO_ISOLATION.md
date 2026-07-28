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

Use `python3 scripts/check_property_repo_isolation.py` for a local diagnostic or `./scripts/propertyquarry_release_python.sh scripts/propertyquarry_release_make_dispatch.py property-release-gates` for authenticated release evidence before a public deploy.
