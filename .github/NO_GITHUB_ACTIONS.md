# No GitHub Actions

PropertyQuarry uses GitHub as a source repository, not as an execution or
release-authority plane.

`ArchonMegalon/propertyquarry` remains the canonical source repository.

Build, test, migration, deployment, health, rollback, and recovery evidence is
produced on the governed local Docker host. The authoritative runtime receipt is
written by `scripts/propertyquarry_local_deployment_receipt.py` after the local
Compose deployment is healthy and bound to the canonical runtime commit and
immutable image IDs.

Adding a `.yml` or `.yaml` file under `.github/workflows/` is a fail-closed
repository-role violation.
