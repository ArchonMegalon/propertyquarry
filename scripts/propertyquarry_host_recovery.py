#!/usr/bin/python3 -I
"""Command-free host-recovery planner for the isolated PropertyQuarry runtime.

Source ``--execute`` is deliberately disabled before configuration or Compose
inspection. Privileged recovery belongs only to the independently installed
native release controller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit


RECEIPT_SCHEMA = "propertyquarry.host_recovery_receipt.v1"
INSTALLED_CONTROLLER_PATH = Path(
    "/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller"
)
SOURCE_EXECUTION_DISABLED = (
    "source --execute is disabled; invoke the installed native controller "
    f"directly as the privileged operator: {INSTALLED_CONTROLLER_PATH} recovery-run"
)
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,127}$")
LEGACY_ALIAS_RE = re.compile(r"(?i)(?<![a-z0-9])ea-(?:api|db|scheduler|worker)(?![a-z0-9])")

APP_ROOT = Path(__file__).resolve().parents[1]
PROPERTY_COMPOSE = APP_ROOT / "docker-compose.property.yml"
TUNNEL_COMPOSE = APP_ROOT / "docker-compose.cloudflared.yml"

SERVICE_CONTAINER = {
    "propertyquarry-db": "propertyquarry-db-live",
    "propertyquarry-api": "propertyquarry-api",
    "propertyquarry-worker": "propertyquarry-worker",
    "propertyquarry-scheduler": "propertyquarry-scheduler",
    "propertyquarry-render-tools": "propertyquarry-render-tools",
    "propertyquarry-cloudflared": "propertyquarry-cloudflared",
}
EPHEMERAL_SERVICE_CONTAINER = {
    "propertyquarry-migrate": "propertyquarry-migrate",
}
EXPECTED_SERVICES = frozenset((*SERVICE_CONTAINER, *EPHEMERAL_SERVICE_CONTAINER))
START_ORDER = tuple(SERVICE_CONTAINER)
CONTAINER_ENV = {
    "PROPERTYQUARRY_DB_CONTAINER_NAME": "propertyquarry-db-live",
    "PROPERTYQUARRY_API_CONTAINER_NAME": "propertyquarry-api",
    "PROPERTYQUARRY_WORKER_CONTAINER_NAME": "propertyquarry-worker",
    "PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME": "propertyquarry-scheduler",
    "PROPERTYQUARRY_RENDER_CONTAINER_NAME": "propertyquarry-render-tools",
    "PROPERTYQUARRY_MIGRATE_CONTAINER_NAME": "propertyquarry-migrate",
    "PROPERTYQUARRY_CLOUDFLARED_CONTAINER_NAME": "propertyquarry-cloudflared",
}
STEP_NAMES = (
    "compose_contract",
    "database_start",
    "database_ready",
    "schema_migration",
    "api_start",
    "api_ready",
    "local_version",
    "worker_start",
    "worker_ready",
    "scheduler_start",
    "scheduler_ready",
    "render_start",
    "render_ready",
    "tunnel_start",
    "tunnel_ready",
    "public_edge",
    "public_version",
)


class RecoveryError(RuntimeError):
    """Base recovery failure."""


class RecoveryValidationError(RecoveryError):
    """Operator input or the dedicated runtime contract is invalid."""


class RecoveryCommandError(RecoveryError):
    """A fixed recovery command or readiness check failed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> CommandResult:
        del argv, env, timeout_seconds
        raise RecoveryValidationError(SOURCE_EXECUTION_DISABLED)


@dataclass(frozen=True)
class RecoveryConfig:
    release_commit_sha: str
    project_name: str
    tunnel_id: str
    route_host: str
    public_origin: str
    local_origin: str
    command_timeout_seconds: int
    ready_timeout_seconds: int
    poll_interval_seconds: float
    command_env: Mapping[str, str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def positive_int(raw: str, *, field_name: str, default: int) -> int:
    value = str(raw or "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) <= 0:
        raise RecoveryValidationError(f"{field_name} must be a positive integer")
    return int(value)


def positive_float(raw: str, *, field_name: str, default: float) -> float:
    value = str(raw or "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RecoveryValidationError(f"{field_name} must be a positive number") from exc
    if parsed <= 0:
        raise RecoveryValidationError(f"{field_name} must be a positive number")
    return parsed


def required_secret(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name) or "").strip()
    if not value:
        raise RecoveryValidationError(f"{name} is required")
    if any(character.isspace() for character in value):
        raise RecoveryValidationError(f"{name} must not contain whitespace")
    if value.lower() in {"change-me", "changeme", "placeholder", "secret", "token"}:
        raise RecoveryValidationError(f"{name} is still a placeholder")
    return value


def validate_route_host(raw: str) -> str:
    host = str(raw or "").strip().lower().rstrip(".")
    if not host:
        raise RecoveryValidationError("PROPERTYQUARRY_RECOVERY_ROUTE_HOST is required")
    parsed = urlsplit(f"https://{host}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RecoveryValidationError(
            "PROPERTYQUARRY_RECOVERY_ROUTE_HOST must be a bare DNS hostname"
        ) from exc
    if parsed.hostname != host or port is not None or parsed.username is not None:
        raise RecoveryValidationError(
            "PROPERTYQUARRY_RECOVERY_ROUTE_HOST must be a bare DNS hostname"
        )
    if host != "propertyquarry.com" and not host.endswith(".propertyquarry.com"):
        raise RecoveryValidationError(
            "PROPERTYQUARRY_RECOVERY_ROUTE_HOST must be propertyquarry.com or its subdomain"
        )
    return host


def validate_dedicated_name(raw: str, *, field_name: str) -> str:
    value = str(raw or "").strip().lower()
    if not SAFE_ID_RE.fullmatch(value) or not value.startswith("propertyquarry-"):
        raise RecoveryValidationError(
            f"{field_name} must be a dedicated propertyquarry-* identifier"
        )
    if LEGACY_ALIAS_RE.search(value):
        raise RecoveryValidationError(f"{field_name} must not contain a legacy EA alias")
    return value


def load_config(environ: Mapping[str, str]) -> RecoveryConfig:
    release = str(environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "").strip().lower()
    if not GIT_SHA_RE.fullmatch(release):
        raise RecoveryValidationError(
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA must be a full 40-character Git SHA"
        )

    project_name = validate_dedicated_name(
        environ.get("PROPERTYQUARRY_COMPOSE_PROJECT_NAME", "propertyquarry-production"),
        field_name="PROPERTYQUARRY_COMPOSE_PROJECT_NAME",
    )
    tunnel_id = validate_dedicated_name(
        environ.get("PROPERTYQUARRY_RECOVERY_TUNNEL_ID", ""),
        field_name="PROPERTYQUARRY_RECOVERY_TUNNEL_ID",
    )
    route_host = validate_route_host(environ.get("PROPERTYQUARRY_RECOVERY_ROUTE_HOST", ""))
    compose_override = str(environ.get("PROPERTYQUARRY_COMPOSE_FILE") or "").strip()
    if compose_override and compose_override not in {
        PROPERTY_COMPOSE.name,
        str(PROPERTY_COMPOSE),
    }:
        raise RecoveryValidationError(
            "PROPERTYQUARRY_COMPOSE_FILE must remain docker-compose.property.yml"
        )

    docker_host = str(environ.get("DOCKER_HOST") or "").strip()
    if docker_host and docker_host != "unix:///var/run/docker.sock":
        raise RecoveryValidationError(
            "DOCKER_HOST must be unset or the local system Docker socket"
        )
    docker_context = str(environ.get("DOCKER_CONTEXT") or "").strip()
    if docker_context and docker_context != "default":
        raise RecoveryValidationError("DOCKER_CONTEXT must be unset or default")

    for name, expected in CONTAINER_ENV.items():
        observed = str(environ.get(name) or expected).strip()
        if observed != expected:
            raise RecoveryValidationError(
                f"{name} must remain {expected}; legacy or shared aliases are forbidden"
            )
        if LEGACY_ALIAS_RE.search(observed):
            raise RecoveryValidationError(f"{name} contains a forbidden legacy EA alias")

    host_port = positive_int(
        environ.get("EA_HOST_PORT", "8090"), field_name="EA_HOST_PORT", default=8090
    )
    if host_port > 65535:
        raise RecoveryValidationError("EA_HOST_PORT must be at most 65535")

    child_env = dict(environ)
    child_env.pop("COMPOSE_FILE", None)
    child_env.pop("COMPOSE_PROJECT_NAME", None)
    # The candidate planner never needs tunnel, database, controller, owner, or
    # migrator credentials.  Recovery secrets live only in the independently
    # installed controller's root-owned store.
    child_env.pop("PROPERTYQUARRY_CF_TUNNEL_TOKEN", None)
    child_env.pop("POSTGRES_PASSWORD", None)
    child_env["PROPERTYQUARRY_COMPOSE_PROJECT_NAME"] = project_name
    child_env.update(CONTAINER_ENV)

    return RecoveryConfig(
        release_commit_sha=release,
        project_name=project_name,
        tunnel_id=tunnel_id,
        route_host=route_host,
        public_origin=f"https://{route_host}",
        local_origin=f"http://127.0.0.1:{host_port}",
        command_timeout_seconds=positive_int(
            environ.get("PROPERTYQUARRY_RECOVERY_COMMAND_TIMEOUT_SECONDS", "30"),
            field_name="PROPERTYQUARRY_RECOVERY_COMMAND_TIMEOUT_SECONDS",
            default=30,
        ),
        ready_timeout_seconds=positive_int(
            environ.get("PROPERTYQUARRY_RECOVERY_READY_TIMEOUT_SECONDS", "300"),
            field_name="PROPERTYQUARRY_RECOVERY_READY_TIMEOUT_SECONDS",
            default=300,
        ),
        poll_interval_seconds=positive_float(
            environ.get("PROPERTYQUARRY_RECOVERY_POLL_INTERVAL_SECONDS", "2"),
            field_name="PROPERTYQUARRY_RECOVERY_POLL_INTERVAL_SECONDS",
            default=2,
        ),
        command_env=child_env,
    )


def expected_confirmation(config: RecoveryConfig) -> str:
    return (
        f"RECOVER PROPERTYQUARRY {config.release_commit_sha} "
        f"VIA {config.tunnel_id} TO {config.route_host}"
    )


def compose_argv(config: RecoveryConfig, *tail: str) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--project-name",
        config.project_name,
        "-f",
        str(PROPERTY_COMPOSE),
        "-f",
        str(TUNNEL_COMPOSE),
        *tail,
    )


def start_argv(config: RecoveryConfig, service: str) -> tuple[str, ...]:
    if service not in EXPECTED_SERVICES:
        raise RecoveryValidationError("refusing to start a non-PropertyQuarry service")
    return compose_argv(
        config, "up", "-d", "--no-build", "--pull", "never", "--no-deps", service
    )


def migration_argv(config: RecoveryConfig) -> tuple[str, ...]:
    service = "propertyquarry-migrate"
    return compose_argv(
        config,
        "up",
        "--no-build",
        "--pull",
        "never",
        "--no-deps",
        "--abort-on-container-exit",
        "--exit-code-from",
        service,
        service,
    )


def inspect_argv(container: str) -> tuple[str, ...]:
    if container not in SERVICE_CONTAINER.values():
        raise RecoveryValidationError("refusing to inspect a non-PropertyQuarry container")
    return (
        "docker",
        "inspect",
        "--format",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
        container,
    )


def migration_inspect_argv() -> tuple[str, ...]:
    return (
        "docker",
        "inspect",
        "--format",
        "{{.State.Status}} {{.State.ExitCode}}",
        EPHEMERAL_SERVICE_CONTAINER["propertyquarry-migrate"],
    )


def curl_argv(url: str) -> tuple[str, ...]:
    scheme = urlsplit(url).scheme
    if scheme not in {"http", "https"}:
        raise RecoveryValidationError("recovery probe URL must use HTTP or HTTPS")
    return (
        "curl",
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--noproxy",
        "*",
        "--proto",
        f"={scheme}",
        "--connect-timeout",
        "5",
        "--max-time",
        "15",
        url,
    )


def output_evidence(value: str) -> dict[str, object]:
    encoded = str(value or "").encode("utf-8", errors="replace")
    return {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}


def atomic_write_receipt(path: Path, payload: Mapping[str, object], *, overwrite: bool) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and not overwrite:
        raise RecoveryValidationError(
            f"receipt already exists: {path}; choose a new path or use --overwrite-receipt"
        )
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def validate_static_compose_contract() -> None:
    for path in (PROPERTY_COMPOSE, TUNNEL_COMPOSE):
        if not path.is_file():
            raise RecoveryValidationError(f"required dedicated Compose file is missing: {path.name}")
        content = path.read_text(encoding="utf-8")
        if LEGACY_ALIAS_RE.search(content):
            raise RecoveryValidationError(
                f"{path.name} contains a forbidden legacy EA service/container alias"
            )
    property_content = PROPERTY_COMPOSE.read_text(encoding="utf-8")
    tunnel_content = TUNNEL_COMPOSE.read_text(encoding="utf-8")
    for service in START_ORDER[:-1]:
        if f"  {service}:" not in property_content:
            raise RecoveryValidationError(f"dedicated Compose contract is missing {service}")
    if "  propertyquarry-migrate:" not in property_content:
        raise RecoveryValidationError(
            "dedicated Compose contract is missing the ephemeral migration service"
        )
    worker_marker = "  propertyquarry-worker:\n"
    scheduler_marker = "  propertyquarry-scheduler:\n"
    worker_start = property_content.find(worker_marker)
    scheduler_start = property_content.find(scheduler_marker, worker_start + len(worker_marker))
    if worker_start < 0 or scheduler_start < 0:
        raise RecoveryValidationError(
            "dedicated Compose contract is missing the durable worker boundary"
        )
    worker_contract = property_content[worker_start:scheduler_start]
    for required_fragment in (
        "EA_ROLE: worker",
        'EA_STORAGE_BACKEND: "postgres"',
        'PROPERTYQUARRY_WORKER_PROFILE: "property_only"',
        'PROPERTYQUARRY_SEARCH_SCHEMA_READINESS_REQUIRED: "1"',
        "propertyquarry_artifacts:/data/artifacts",
        "propertyquarry-migrate:",
        "condition: service_completed_successfully",
        'test: ["CMD", "/usr/local/bin/python", "-m", "app.scheduler_healthcheck"]',
        "read_only: true",
    ):
        if required_fragment not in worker_contract:
            raise RecoveryValidationError(
                "durable worker Compose contract is incomplete"
            )
    if any(
        forbidden in worker_contract
        for forbidden in (
            "property_scene_video_shared.env",
            "propertyquarry_render_internal",
        )
    ):
        raise RecoveryValidationError(
            "durable worker must remain outside the optional advanced-visual boundary"
        )
    if (
        'command: ["/usr/local/bin/python", "-m", "app.product.propertyquarry_schema", "migrate"]'
        not in property_content
    ):
        raise RecoveryValidationError(
            "ephemeral migration service does not run the governed migration command"
        )
    if property_content.count("condition: service_completed_successfully") < 3:
        raise RecoveryValidationError(
            "API, worker, and scheduler must require successful schema migration"
        )
    if "  propertyquarry-cloudflared:" not in tunnel_content:
        raise RecoveryValidationError("dedicated tunnel Compose contract is missing cloudflared")
    if "PROPERTYQUARRY_CF_TUNNEL_TOKEN" not in tunnel_content:
        raise RecoveryValidationError("dedicated tunnel Compose contract does not require its token")


def run_fixed(
    runner: CommandRunner,
    config: RecoveryConfig,
    step: str,
    argv: Sequence[str],
    *,
    timeout_seconds: int | None = None,
) -> CommandResult:
    del runner, config, step, argv, timeout_seconds
    raise RecoveryValidationError(SOURCE_EXECUTION_DISABLED)


def version_from_output(output: str) -> str:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RecoveryCommandError("/version did not return one JSON object") from exc
    if not isinstance(payload, dict):
        raise RecoveryCommandError("/version did not return one JSON object")
    release = str(payload.get("release_commit_sha") or "").strip().lower()
    if not GIT_SHA_RE.fullmatch(release):
        raise RecoveryCommandError("/version did not return a full release_commit_sha")
    return release


def wait_for_container(
    *,
    runner: CommandRunner,
    config: RecoveryConfig,
    step: str,
    container: str,
    require_healthy: bool,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
) -> CommandResult:
    del runner, config, step, container, require_healthy, monotonic, sleeper
    raise RecoveryValidationError(SOURCE_EXECUTION_DISABLED)


def wait_for_http(
    *,
    runner: CommandRunner,
    config: RecoveryConfig,
    step: str,
    url: str,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
) -> CommandResult:
    del runner, config, step, url, monotonic, sleeper
    raise RecoveryValidationError(SOURCE_EXECUTION_DISABLED)


def blank_receipt(*, execute: bool) -> dict[str, object]:
    return {
        "schema": RECEIPT_SCHEMA,
        "generated_at": isoformat(utc_now()),
        "mode": "execute" if execute else "dry_run",
        "status": "initializing",
        "execution_ready": False,
        "identity": {},
        "dedicated_boundary": {
            "compose_files": [PROPERTY_COMPOSE.name, TUNNEL_COMPOSE.name],
            "services": list(START_ORDER),
            "declared_services": sorted(EXPECTED_SERVICES),
            "steady_state_services": list(START_ORDER),
            "ephemeral_services": ["propertyquarry-migrate"],
            "legacy_ea_aliases_allowed": False,
            "credentials_owned_by_external_controller": True,
        },
        "external_controller": {
            "required": True,
            "operation": "recovery-run",
            "source_execution_enabled": False,
            "authority": "installed_native_controller",
        },
        "candidate_observations": {
            "authoritative": False,
            "compose_contract": {
                "status": "not_checked",
                "issues": [],
            },
        },
        "confirmation": {"required": execute, "matched": False},
        "mutation_boundary": {
            "service_start_attempted": False,
            "service_start_completed": [],
            "migration_attempted": False,
            "migration_completed": False,
        },
        "verification": {
            "database": False,
            "migration": False,
            "api": False,
            "local_version": False,
            "worker": False,
            "scheduler": False,
            "render": False,
            "tunnel": False,
            "public_edge": False,
            "public_version": False,
        },
        "steps": [],
    }


def run_recovery(
    *,
    environ: Mapping[str, str],
    receipt_path: Path,
    execute: bool = False,
    confirmation: str = "",
    overwrite_receipt: bool = False,
    runner: CommandRunner | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, object], int]:
    receipt = blank_receipt(execute=execute)
    if execute:
        receipt["status"] = "failed"
        receipt["error"] = {
            "type": "SourceExecutionDisabled",
            "message": SOURCE_EXECUTION_DISABLED,
        }
        receipt["completed_at"] = isoformat(utc_now())
        atomic_write_receipt(receipt_path, receipt, overwrite=overwrite_receipt)
        return receipt, 2

    exit_code = 0
    active_step: dict[str, object] | None = None
    runner = runner or SubprocessCommandRunner()
    try:
        config = load_config(environ)
        compose_observation = receipt["candidate_observations"]
        assert isinstance(compose_observation, dict)
        compose_contract = compose_observation["compose_contract"]
        assert isinstance(compose_contract, dict)
        try:
            validate_static_compose_contract()
        except RecoveryValidationError as exc:
            compose_contract["status"] = "advisory_issue"
            compose_contract["issues"] = [str(exc)]
        else:
            compose_contract["status"] = "observed"
        receipt["identity"] = {
            "release_commit_sha": config.release_commit_sha,
            "compose_project": config.project_name,
            "tunnel_id": config.tunnel_id,
            "route_host": config.route_host,
            "public_origin": config.public_origin,
            "local_origin": config.local_origin,
        }
        dedicated = receipt["dedicated_boundary"]
        assert isinstance(dedicated, dict)
        receipt["steps"] = [
            {
                "name": "external_controller.recovery-run",
                "status": "planned",
                "owns": [
                    "fixed_lock",
                    "external_monotonic_state",
                    "journal_containment",
                    "database_fence",
                    "canonical_compose_plan",
                    "traffic_and_verification",
                ],
            }
        ]
        expected = expected_confirmation(config)
        confirmation_receipt = receipt["confirmation"]
        assert isinstance(confirmation_receipt, dict)
        confirmation_receipt["expected_sha256"] = hashlib.sha256(expected.encode()).hexdigest()

        receipt["status"] = "dry_run"
        return receipt, 0
    except RecoveryValidationError as exc:
        exit_code = 2
        receipt["status"] = "failed"
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if active_step is not None and active_step.get("status") == "running":
            active_step["status"] = "failed"
    except RecoveryError as exc:
        exit_code = 1
        receipt["status"] = "failed"
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if active_step is not None and active_step.get("status") == "running":
            active_step["status"] = "failed"
    except Exception:
        exit_code = 1
        receipt["status"] = "failed"
        receipt["error"] = {
            "type": "UnexpectedRecoveryError",
            "message": "unexpected recovery failure; sensitive details were withheld",
        }
        if active_step is not None and active_step.get("status") == "running":
            active_step["status"] = "failed"
    finally:
        receipt["completed_at"] = isoformat(utc_now())
        atomic_write_receipt(receipt_path, receipt, overwrite=overwrite_receipt)
    return receipt, exit_code


def default_receipt_path() -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return APP_ROOT / "_completion" / "propertyquarry_host_recovery" / f"{stamp}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover only the dedicated PropertyQuarry runtime after a host reboot."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="fail closed; source execution is disabled and the installed controller is required",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="exact release/tunnel/route-bound confirmation phrase required with --execute",
    )
    parser.add_argument("--receipt", type=Path, default=None, help="atomic JSON receipt path")
    parser.add_argument(
        "--overwrite-receipt",
        action="store_true",
        help="permit replacing an existing receipt path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt_path = args.receipt or default_receipt_path()
    try:
        receipt, exit_code = run_recovery(
            environ=os.environ,
            receipt_path=receipt_path,
            execute=args.execute,
            confirmation=args.confirm,
            overwrite_receipt=args.overwrite_receipt,
        )
    except RecoveryValidationError as exc:
        print(f"PropertyQuarry recovery receipt error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"status": receipt.get("status"), "receipt": str(receipt_path)},
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
