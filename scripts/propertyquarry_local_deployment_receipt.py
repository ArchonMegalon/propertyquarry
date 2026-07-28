#!/usr/bin/env python3
"""Audit the authoritative PropertyQuarry Docker-local deployment.

The receipt deliberately records no provider credentials, database URLs, or
container environment beyond the non-secret release identity fields. GitHub is
only a source remote; it is never consulted and is never release authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Mapping, Sequence


ROOT: Final = Path(__file__).resolve().parents[1]
SCHEMA: Final = "propertyquarry.local_docker_deployment.v1"
REPOSITORY: Final = "ArchonMegalon/propertyquarry"
DEFAULT_RECEIPT: Final = (
    ROOT / "state/release/propertyquarry-local-deployment.v1.json"
)
FULL_SHA: Final = re.compile(r"[0-9a-f]{40}\Z")
DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
PROJECT: Final = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}\Z")
COMPOSE_FILES: Final = (
    Path("docker-compose.property.yml"),
    Path("docker-compose.cloudflared.yml"),
)
SERVICE_CONTRACT: Final = {
    "propertyquarry-api": "healthy",
    "propertyquarry-migrate": "completed",
    "propertyquarry-worker": "healthy",
    "propertyquarry-scheduler": "healthy",
    "propertyquarry-render-tools": "running",
    "propertyquarry-db": "healthy",
    "propertyquarry-cloudflared": "running",
}
WEB_SERVICES: Final = frozenset(
    {
        "propertyquarry-api",
        "propertyquarry-migrate",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
    }
)
RELEASE_BOUND_SERVICES: Final = WEB_SERVICES | {"propertyquarry-render-tools"}
HEALTHY_SERVICES: Final = frozenset(
    {
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-db",
    }
)
RELEASE_ENV_ALLOWLIST: Final = frozenset(
    {
        "PROPERTYQUARRY_RELEASE_REPOSITORY",
        "PROPERTYQUARRY_RELEASE_BRANCH",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST",
        "PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID",
        "PROPERTYQUARRY_RELEASE_PUBLIC_ORIGIN",
        "PROPERTYQUARRY_RELEASE_ARTIFACT_SET",
        "PROPERTYQUARRY_RELEASE_LABEL",
        "PROPERTYQUARRY_RELEASE_GENERATED_AT",
    }
)


class DeploymentAuditError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
        ):
            raise DeploymentAuditError(f"compose_file_invalid:{path.name}")
        digest = hashlib.sha256()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise DeploymentAuditError(f"compose_file_short_read:{path.name}")
            digest.update(chunk)
            remaining -= len(chunk)
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
    )
    if result.returncode != 0:
        raise DeploymentAuditError("git_identity_unavailable")
    return result.stdout.strip()


def _environment(values: object) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(values, list):
        return result
    for row in values:
        if not isinstance(row, str) or "=" not in row:
            continue
        key, value = row.split("=", 1)
        if key in RELEASE_ENV_ALLOWLIST:
            result[key] = value
    return result


def _container_ids(project: str, service: str) -> list[str]:
    result = _run(
        (
            "/usr/bin/docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.ID}}",
        )
    )
    if result.returncode != 0:
        raise DeploymentAuditError("docker_container_query_failed")
    return [row for row in result.stdout.splitlines() if row]


def _inspect(container_id: str) -> dict[str, object]:
    result = _run(("/usr/bin/docker", "inspect", container_id))
    if result.returncode != 0:
        raise DeploymentAuditError("docker_container_inspect_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentAuditError("docker_container_inspect_invalid") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise DeploymentAuditError("docker_container_inspect_shape_invalid")
    item = payload[0]
    if not isinstance(item, dict):
        raise DeploymentAuditError("docker_container_inspect_shape_invalid")
    return item


def _probe(
    origin: str,
    *,
    expected_commit: str,
    expected_web_image: str,
) -> dict[str, object]:
    target = origin.rstrip("/") + "/health/ready"
    version_target = origin.rstrip("/") + "/version"
    request = urllib.request.Request(
        target,
        headers={"Host": "propertyquarry.com", "User-Agent": "pq-local-release/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read(65_536)
            readiness_status = int(response.status)
        version_request = urllib.request.Request(
            version_target,
            headers={
                "Host": "propertyquarry.com",
                "User-Agent": "pq-local-release/1",
            },
            method="GET",
        )
        with urllib.request.urlopen(version_request, timeout=15) as response:
            version_status = int(response.status)
            version_raw = response.read(65_537)
        if len(version_raw) > 65_536:
            raise ValueError("version_response_too_large")
        version = json.loads(version_raw)
        if not isinstance(version, dict):
            raise ValueError("version_response_invalid")
    except (json.JSONDecodeError, OSError, urllib.error.URLError, ValueError):
        return {
            "status": "failed",
            "http_status": 0,
            "target": target,
            "version_target": version_target,
        }
    identity = {
        "release_commit_sha": str(version.get("release_commit_sha") or ""),
        "release_image_digest": str(version.get("release_image_digest") or ""),
        "release_manifest_status": str(
            version.get("release_manifest_status") or ""
        ),
        "release_manifest_sha256": str(
            version.get("release_manifest_sha256") or ""
        ),
    }
    passed = (
        readiness_status == 200
        and version_status == 200
        and identity["release_commit_sha"] == expected_commit
        and identity["release_image_digest"] == expected_web_image
        and identity["release_manifest_status"] == "complete"
        and re.fullmatch(
            r"[0-9a-f]{64}",
            identity["release_manifest_sha256"],
        )
        is not None
    )
    return {
        "status": "pass" if passed else "failed",
        "http_status": readiness_status,
        "target": target,
        "version_http_status": version_status,
        "version_target": version_target,
        "release_identity": identity,
    }


def _public_tour_volume_privacy(container_name: str) -> dict[str, object]:
    result = _run(
        (
            "/usr/bin/docker",
            "exec",
            container_name,
            "python",
            "-m",
            "scripts.propertyquarry_public_tour_volume_privacy",
            "--root",
            "/data/public_property_tours",
        )
    )
    if result.returncode not in {0, 1} or len(result.stdout.encode()) > 65_536:
        return {"status": "failed", "secret_values_recorded": False}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "failed", "secret_values_recorded": False}
    if (
        not isinstance(payload, dict)
        or payload.get("schema")
        != "propertyquarry-public-tour-volume-privacy-v1"
        or payload.get("secret_values_recorded") is not False
    ):
        return {"status": "failed", "secret_values_recorded": False}
    counts = payload.get("counts")
    safe_counts = (
        {
            str(key): int(value)
            for key, value in counts.items()
            if isinstance(key, str) and isinstance(value, int)
        }
        if isinstance(counts, dict)
        else {}
    )
    return {
        "status": str(payload.get("status") or "failed"),
        "mode": str(payload.get("mode") or ""),
        "counts": safe_counts,
        "secret_values_recorded": False,
    }


def audit_local_deployment(
    *,
    root: Path = ROOT,
    expected_commit: str,
    expected_web_image: str,
    expected_render_image: str,
    compose_project: str = "property",
    local_origin: str = "http://127.0.0.1:8097",
) -> dict[str, object]:
    root = root.resolve(strict=True)
    failures: list[str] = []
    if FULL_SHA.fullmatch(expected_commit) is None:
        raise DeploymentAuditError("expected_commit_invalid")
    if DIGEST.fullmatch(expected_web_image) is None:
        raise DeploymentAuditError("expected_web_image_invalid")
    if DIGEST.fullmatch(expected_render_image) is None:
        raise DeploymentAuditError("expected_render_image_invalid")
    if PROJECT.fullmatch(compose_project) is None:
        raise DeploymentAuditError("compose_project_invalid")
    envelope_head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    ancestor = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            expected_commit,
            envelope_head,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
        },
    )
    if ancestor.returncode != 0:
        failures.append("runtime_commit_not_envelope_ancestor")

    compose = {
        path.as_posix(): _sha256(root / path) for path in COMPOSE_FILES
    }
    services: dict[str, object] = {}
    for service, required_state in SERVICE_CONTRACT.items():
        identifiers = _container_ids(compose_project, service)
        if len(identifiers) != 1:
            failures.append(f"{service}:container_cardinality")
            continue
        item = _inspect(identifiers[0])
        config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
        host = (
            item.get("HostConfig")
            if isinstance(item.get("HostConfig"), dict)
            else {}
        )
        state = item.get("State") if isinstance(item.get("State"), dict) else {}
        labels = (
            config.get("Labels")
            if isinstance(config.get("Labels"), dict)
            else {}
        )
        release = _environment(config.get("Env"))
        health_value = state.get("Health")
        health = (
            str(health_value.get("Status") or "")
            if isinstance(health_value, dict)
            else "none"
        )
        status = str(state.get("Status") or "")
        exit_code = int(state.get("ExitCode") or 0)
        image_id = str(item.get("Image") or "")
        if labels.get("com.docker.compose.project") != compose_project:
            failures.append(f"{service}:compose_project_mismatch")
        if labels.get("com.docker.compose.service") != service:
            failures.append(f"{service}:compose_service_mismatch")
        if required_state == "healthy" and not (
            status == "running" and health == "healthy"
        ):
            failures.append(f"{service}:not_healthy")
        elif required_state == "running" and status != "running":
            failures.append(f"{service}:not_running")
        elif required_state == "completed" and not (
            status == "exited" and exit_code == 0
        ):
            failures.append(f"{service}:migration_not_completed")
        if service in WEB_SERVICES and image_id != expected_web_image:
            failures.append(f"{service}:web_image_mismatch")
        if (
            service == "propertyquarry-render-tools"
            and image_id != expected_render_image
        ):
            failures.append("propertyquarry-render-tools:render_image_mismatch")
        if service in RELEASE_BOUND_SERVICES:
            if (
                release.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA")
                != expected_commit
            ):
                failures.append(f"{service}:release_commit_mismatch")
            if (
                release.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST")
                != expected_web_image
            ):
                failures.append(f"{service}:release_image_binding_mismatch")
            if str(config.get("User") or "") != "10001:10001":
                failures.append(f"{service}:runtime_user_mismatch")
        if host.get("Privileged") is True:
            failures.append(f"{service}:privileged")
        security_options = host.get("SecurityOpt")
        if service != "propertyquarry-db" and (
            not isinstance(security_options, list)
            or not any(
                str(value).endswith("no-new-privileges")
                or str(value) == "no-new-privileges:true"
                for value in security_options
            )
        ):
            failures.append(f"{service}:no_new_privileges_missing")
        mounts = item.get("Mounts") if isinstance(item.get("Mounts"), list) else []
        if any(
            isinstance(mount, dict)
            and str(mount.get("Destination") or "")
            in {"/var/run/docker.sock", "/run/docker.sock"}
            for mount in mounts
        ):
            failures.append(f"{service}:docker_socket_mounted")
        services[service] = {
            "container_id": str(item.get("Id") or ""),
            "container_name": str(item.get("Name") or "").lstrip("/"),
            "image_id": image_id,
            "status": status,
            "health": health,
            "exit_code": exit_code,
            "release_identity": release,
        }

    local_probe = _probe(
        local_origin,
        expected_commit=expected_commit,
        expected_web_image=expected_web_image,
    )
    if local_probe["status"] != "pass":
        failures.append("local_readiness_probe_failed")
    api_service = services.get("propertyquarry-api")
    api_container_name = (
        str(api_service.get("container_name") or "")
        if isinstance(api_service, dict)
        else ""
    )
    public_tour_volume_privacy = (
        _public_tour_volume_privacy(api_container_name)
        if api_container_name
        else {"status": "failed", "secret_values_recorded": False}
    )
    if public_tour_volume_privacy["status"] != "pass":
        failures.append("public_tour_volume_privacy_failed")
    unique_failures = list(dict.fromkeys(failures))
    return {
        "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "authority": {
            "scope": "local_docker",
            "proof_plane": "local_docker_operator_receipts",
            "github_actions_used": False,
            "canonical_repository": REPOSITORY,
        },
        "runtime_commit_sha": expected_commit,
        "envelope_head_sha": envelope_head,
        "images": {
            "web": expected_web_image,
            "render": expected_render_image,
        },
        "compose": {
            "project": compose_project,
            "files": compose,
        },
        "services": services,
        "local_probe": local_probe,
        "public_tour_volume_privacy": public_tour_volume_privacy,
        "secret_values_recorded": False,
        "passed": not unique_failures,
        "failures": unique_failures,
    }


def _write(path: Path, receipt: Mapping[str, object]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit and receipt the authoritative local Docker deployment."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-web-image", required=True)
    parser.add_argument("--expected-render-image", required=True)
    parser.add_argument("--compose-project", default="property")
    parser.add_argument("--local-origin", default="http://127.0.0.1:8097")
    parser.add_argument("--write", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args(argv)
    try:
        receipt = audit_local_deployment(
            root=args.root,
            expected_commit=args.expected_commit,
            expected_web_image=args.expected_web_image,
            expected_render_image=args.expected_render_image,
            compose_project=args.compose_project,
            local_origin=args.local_origin,
        )
    except (OSError, DeploymentAuditError) as exc:
        print(f"local Docker deployment audit could not complete: {exc}", file=sys.stderr)
        return 1
    _write(args.write, receipt)
    if not receipt["passed"]:
        print(
            "local Docker deployment audit failed: "
            + ", ".join(str(value) for value in receipt["failures"]),
            file=sys.stderr,
        )
        return 2
    print(
        "ok: local Docker deployment is healthy and bound to "
        f"{args.expected_commit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
