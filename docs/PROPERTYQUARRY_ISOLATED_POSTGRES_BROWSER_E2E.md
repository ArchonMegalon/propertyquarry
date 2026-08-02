# Isolated PostgreSQL browser E2E

`scripts/smoke_property_postgres_isolated.py` is the host-safe PostgreSQL
browser lane. It is intentionally separate from `scripts/smoke_postgres.sh`
and the production Compose project.

## Preconditions

- Run from a disposable integration worktree, never `/docker/property`.
- Supply an absolute, already-provisioned Python virtual environment with the
  runtime, pytest, and Playwright dependencies. It may be outside the
  candidate worktree (for example `/docker/property/.venv`), but the candidate
  `--repo-root` itself may never be `/docker/property`. The gate validates the
  venv directory, `pyvenv.cfg`, `bin/`, and executable before entering the
  scope. The additional local packages needed by this host are an explicit
  name/version profile, not proof of `ea/requirements.lock` or a production
  lockfile. The gate resolves their caller-owned user-site source without
  following a raw symlink chain, hashes every distribution-owned file, and
  copies the exact 106.3 MB (101.4 MiB) snapshot into a private per-run `PYTHONUSERBASE`
  capped at 128 MiB. Overlay files and directories are never group/world
  writable and are re-hashed with the source at cleanup. The venv remains
  earlier than this user-site overlay on Python's normal import path;
  `PYTHONPATH` contains only the candidate `ea/` source. The gate never
  inherits an arbitrary `PYTHONPATH`, installs, or downloads anything.
- Supply `--chromium-headless-shell` with the absolute, canonical path to an
  already-installed Playwright Chromium headless-shell executable. The gate
  accepts only Playwright's
  `chromium_headless_shell-<revision>/chrome-headless-shell-linux64/chrome-headless-shell`
  layout and validates a caller-owned, single-link, executable ELF file with
  no symlinked leaf or ancestor. A missing or generic full-Chrome executable
  fails closed. The gate never installs a browser, searches a browser cache,
  selects a channel, or falls back to Playwright's default executable.
- The digest-pinned `postgres:16-alpine` image recorded in the script must
  already be present in the local Docker store. The gate resolves it to an
  immutable image ID and uses `--pull never`.
- A user systemd manager and `/usr/bin/systemd-run` are required. There is no
  uncapped fallback.

From the integration worktree, run:

```bash
export PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE=/absolute/path/to/chrome-headless-shell
python3 scripts/smoke_property_postgres_isolated.py \
  --repo-root "$(pwd)" \
  --venv "$(pwd)/.venv" \
  --chromium-headless-shell "${PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE}"
```

Set the environment value to the canonical executable already installed on the
current host. The gate performs no browser discovery and has no default or
fallback executable; no user-specific cache location is part of the release
contract.

The launcher re-executes the candidate venv Python under a transient user
scope capped at 1 GiB RAM, zero additional swap, 128 tasks, one CPU, and 20
minutes. The process inside that scope verifies the effective cgroup values
and fails closed if any limit is absent or looser. An internal 14-minute
watchdog raises through the normal resource `finally` path, leaving six
minutes before systemd's `RuntimeMaxSec` backup. The coded worst-case critical
cleanup budget is 265 seconds (15 Docker operations at 15 seconds, 20 seconds
for API termination, four seconds for an interrupted producer group, 10
seconds for relay joins, and six seconds for the storage guard), with a further
60-second safety margin inside that reserve. SIGTERM
and SIGINT use the
same unwind path and further termination signals are ignored during exact
cleanup. The 128-task ceiling leaves bounded headroom above the observed
96-task Chromium-plus-relay peak; the former 96-task ceiling denied one task
and stalled the browser journey. Memory, swap, CPU, and runtime ceilings remain
unchanged.

## Docker boundary

Each run generates one 16-hex run ID and uses only:

```text
pq-pg-e2e-<run-id>-db
pq-pg-e2e-<run-id>-net
pq-pg-e2e-<run-id>-data
propertyquarry.postgres-browser-e2e.run=<run-id>
```

The database container is limited to 512 MiB RAM, memory-plus-swap of 512 MiB,
one CPU, and 128 PIDs. It has a Docker healthcheck and remains attached only to
an internal bridge. Docker 29 does not materialize host port mappings for an
internal bridge, so the gate does not request a misleading published port.
Instead, it verifies the container's RFC1918 address and network ID against the
exact named attachment, then starts a bounded in-process TCP relay on a
Docker-assigned random `127.0.0.1` port. The relay allows at most eight active
connections, uses bounded buffers and timeouts, and is stopped and joined
before Docker cleanup. The unique labeled volume uses the local driver's tmpfs
backend with a hard 256 MiB size plus `nosuid,nodev,noexec`. The volume,
network, and container are fail-closed against exact-name and label collisions
before creation.

The gate does not use Compose, a PropertyQuarry runtime image, a scheduler,
the incoming-tour directory, a repository `.env`, a fixed host port, image
builds, image pulls, `latest`, or any Docker prune operation. Migrations, schema
check, the candidate API, session bootstrap, and the existing PostgreSQL
Playwright test all run from the integration worktree and venv. Runtime state
and secrets live under a mode-0700 temporary directory; both generated env
files are mode 0600. Because scheduler and worker processes are deliberately
outside this API/browser lane, their topology-heartbeat requirements are
explicitly disabled only in the private candidate environment; database,
startup-gate, admission, ingress, and schema readiness remain enforced. The
separate deployment gate remains authoritative for multi-role heartbeat
readiness. Production runtime settings include a fresh, per-run property-search
erasure secret. Before migration, the controller creates the
exact dedicated `NOLOGIN NOINHERIT` admission-capacity owner inside the
disposable cluster and verifies that all elevated flags and outbound
memberships are absent. After migration, it creates distinct
`propertyquarry_api_admission` and `propertyquarry_api_ingress` logins with
independent fresh per-run passwords, removes public
database/schema/relation/function authority, and explicitly denies each login
access to the other authority's tables. The internal role receives only
admission-table `SELECT, INSERT, UPDATE, DELETE` plus capacity-state `SELECT`;
the ingress role receives the corresponding rights only on the bounded ingress
quota/lease tables plus ingress-capacity `SELECT`. Both roles are proved to be
unprivileged, membership-free logins, the internal role runs the production
strict least-privilege probe, and the ingress role runs an exact privilege and
capacity-contract probe before API startup. The disposable ingress relations,
security-definer capacity triggers, hard row ceilings, cross-revocations, and
grants are built from the same parameter-validated SQL generator used by the
production admission-database provisioner. The production-mode API receives
both dedicated DSNs,
neither of which equals the primary application DSN or each other. No database
DSN or password is placed in Docker or process argv. The controller derives
PostgreSQL SCRAM verifiers client-side, so clear passwords are not embedded in
loggable role-management statements. Before any direct libpq connection, the
scoped controller removes all inherited `PG*` overrides, asserts that the
environment remains closed, and explicitly binds `hostaddr=127.0.0.1`, empty
session options, and the private relay port from the canonical DSNs.
Migration, schema-check, session-bootstrap, the PostgreSQL queue/erasure
ordering contract tests, browser-test, and API stdout/stderr are routed only to
distinct mode-0600 temporary logs; the terminal receives generic pass/fail
status only.
Every host producer is launched through `/usr/bin/prlimit`. Migration,
schema-check, session-bootstrap, and API producers retain an 8 MiB file-size
ceiling. The browser-test producer alone receives 128 MiB because Chromium's
`--disable-dev-shm-usage` shared-memory backing is a TMPDIR file and inherits
the producer limit. This does not loosen logs: browser stdout/stderr flows over
a pipe to the controller, which writes at most 8 MiB to its mode-0600 log and
terminates the registered browser group on overflow; all other producer logs
remain directly protected by the 8 MiB file limit. A separate monitor accounts
for every file below the private run root, including nested files and the
dependency overlay, with a 512 MiB aggregate ceiling. It counts symlink entries
without traversing their targets, accounts allocated filesystem blocks as well
as logical size, and permits at most 16,384 filesystem entries. Directory walks
are streamed rather than materialized. Only explicitly registered API/
migration/schema/bootstrap/browser producer process groups can be terminated
by this monitor; it applies bounded TERM then KILL waits. Docker inventory and
cleanup commands are never registered or placed in those groups.

## Secret-free failure phases

Subprocess failures expose only an allowlisted phase and reason, never the
command, stdout, stderr, URL, database DSN, token, or generated secret. Docker
phases cover `docker-preflight-{container,network,volume}-{name,label}` plus
`docker-image-inspect`, network/volume/container create and label checks,
health inspection, exact internal-network address inspection, fixed-argv
capacity-owner bootstrap and verification, and bounded loopback-relay
lifecycle. Host phases are
`schema-migrate`, `schema-check`, `session-bootstrap`, and `browser-test`.
API startup/readiness and exact per-resource cleanup have dedicated phase
codes as well.

Command reasons are limited to:

```text
execution-failed
timeout
exit-nonzero
stderr-not-empty
stdout-too-large
collision
log-invalid
```

Docker output phases allow only the first five reasons, host-log phases allow
only `execution-failed`, `exit-nonzero`, and `log-invalid`, and `collision` is
valid only for the preflight and final inventory phases.

Known semantic failures, such as an invalid image ID, label mismatch,
unhealthy container, invalid bootstrap receipt, or API readiness timeout, use
equally fixed codes. A successful command that writes any Docker stderr still
fails closed as `stderr-not-empty`; no harmless stderr pattern has been proven
and none is allowlisted.

The outer launcher sends scoped stderr only to a newly created mode-0600 file
inside a mode-0700 temporary directory. It propagates a phase code only when
the entire file is one exact ASCII failure line and the code belongs to the
compiled allowlist. Extra bytes, tracebacks, oversized content, unknown codes,
wrong modes, hard links, or symlinks collapse to `scoped-run-failed`; their
contents are never printed. The private diagnostic is removed with its
temporary directory after classification.

The validated executable is passed to the existing browser test through the
single `PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE` override. That test
launches only `playwright.chromium` with `executable_path` set to the exact
validated headless-shell file and a fixed, non-value-bearing argument tuple.
Inherited browser-cache variables, full Chrome, browser channels, Firefox,
WebKit, and generic Playwright executable discovery are outside this lane.

The bootstrap receipt is rejected unless it is a single-link, caller-owned,
regular non-symlink file, mode 0600, non-empty, at most 1 MiB, and contains the
expected passing internal-session contract and access token. Its validated
bytes are copied into the mode-0700 run directory, revalidated, and only that
private copy is given to Playwright.

## Cleanup

The candidate API is terminated first, the producer-only storage guard is
stopped, and then the database relay is stopped with all connection workers
joined. Before removing any Docker object,
the gate reads its label and requires the exact run ID. It then removes only
the exact container name, exact network name, and exact volume name, in that
order, and repeats the name/label inventory to prove they are gone. It never
targets the `property` Compose project or any `propertyquarry-api`, scheduler,
live DB, public-tour, artifact, or provider-ledger object. A label mismatch or
leftover
resource fails the gate and leaves the mismatched object untouched for manual
inspection. Each cleanup inspect/remove command has a 15-second ceiling. This
replaces the original 5-second bound, which was too narrow for a capped host to
force-stop a container and let Docker finish its local bookkeeping. A Docker
cleanup failure has priority over dependency, storage-guard, or relay-stop
errors so a leftover disposable resource cannot be hidden. The 14-minute
internal watchdog and 20-minute scope deadline remain the aggregate bounds.
Dependency source and overlay evidence is re-hashed only after relay shutdown
and exact Docker cleanup, so evidence verification can never delay removal of
the critical disposable resources.
