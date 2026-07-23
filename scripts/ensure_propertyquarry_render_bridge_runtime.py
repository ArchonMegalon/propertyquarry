#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = "docker-compose.property.yml"
DEFAULT_RENDER_SERVICE = "propertyquarry-render-tools"
DEFAULT_RENDER_CONTAINER = "propertyquarry-render-tools"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8091/health"
CONTRACT_NAME = "propertyquarry.render_bridge_runtime.v1"
CRITICAL_RUNTIME_SCRIPT_PATHS = (
    (ROOT / "scripts" / "generate_property_reconstruction.py", "/app/scripts/generate_property_reconstruction.py"),
    (ROOT / "scripts" / "property_reconstruction_styles.py", "/app/scripts/property_reconstruction_styles.py"),
    (ROOT / "scripts" / "property_reconstruction_render_bridge.py", "/app/scripts/property_reconstruction_render_bridge.py"),
    (ROOT / "vendor" / "three" / "0.167.1" / "three.module.js", "/app/vendor/three/0.167.1/three.module.js"),
    (
        ROOT / "vendor" / "three" / "0.167.1" / "examples" / "jsm" / "controls" / "OrbitControls.js",
        "/app/vendor/three/0.167.1/examples/jsm/controls/OrbitControls.js",
    ),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def _docker_available() -> bool:
    return bool(shutil.which("docker"))


def _compose_command(*, compose_file: str, compose_project_name: str = "") -> list[str] | None:
    if not _docker_available():
        return None
    if _run(["docker", "compose", "version"], timeout=10).returncode == 0:
        command = ["docker", "compose"]
        if compose_project_name:
            command.extend(["-p", compose_project_name])
        command.extend(["-f", compose_file])
        return command
    if shutil.which("docker-compose") and _run(["docker-compose", "version"], timeout=10).returncode == 0:
        command = ["docker-compose"]
        if compose_project_name:
            command.extend(["-p", compose_project_name])
        command.extend(["-f", compose_file])
        return command
    return None


def _container_state(container: str) -> dict[str, object]:
    normalized_container = str(container or "").strip()
    if not normalized_container or not _docker_available():
        return {"exists": False, "status": "", "health": "", "detail": ""}
    inspected = _run(["docker", "inspect", normalized_container], timeout=20)
    if inspected.returncode != 0 or not str(inspected.stdout or "").strip():
        return {
            "exists": False,
            "status": "",
            "health": "",
            "detail": str(inspected.stderr or inspected.stdout or "").strip()[-500:],
        }
    try:
        payload = json.loads(inspected.stdout)
    except Exception as exc:
        return {
            "exists": False,
            "status": "",
            "health": "",
            "detail": f"{type(exc).__name__}:{exc}",
        }
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return {"exists": False, "status": "", "health": "", "detail": "inspect_payload_invalid"}
    row = payload[0]
    state = dict(row.get("State") or {})
    health = dict(state.get("Health") or {})
    return {
        "exists": True,
        "status": str(state.get("Status") or "").strip(),
        "health": str(health.get("Status") or "none").strip() or "none",
        "detail": "",
    }


def _container_health_probe(container: str, *, health_url: str) -> dict[str, object]:
    normalized_container = str(container or "").strip()
    normalized_url = str(health_url or "").strip()
    if not normalized_container:
        return {"status": "blocked", "reason": "container_missing"}
    if not normalized_url:
        return {"status": "blocked", "reason": "health_url_missing"}
    completed = _run(
        [
            "docker",
            "exec",
            normalized_container,
            "sh",
            "-lc",
            f"curl -fsS --connect-timeout 2 --max-time 10 {normalized_url} >/dev/null",
        ],
        timeout=20,
    )
    if completed.returncode == 0:
        return {"status": "pass", "url": normalized_url}
    return {
        "status": "failed",
        "url": normalized_url,
        "stdout_tail": str(completed.stdout or "")[-400:],
        "stderr_tail": str(completed.stderr or "")[-400:],
    }


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _container_path_sha256(container: str, container_path: str) -> dict[str, object]:
    normalized_container = str(container or "").strip()
    normalized_path = str(container_path or "").strip()
    if not normalized_container:
        return {"status": "blocked", "reason": "container_missing"}
    if not normalized_path:
        return {"status": "blocked", "reason": "container_path_missing"}
    completed = _run(
        [
            "docker",
            "exec",
            normalized_container,
            "python",
            "-c",
            (
                "import hashlib, pathlib, sys; "
                "path = pathlib.Path(sys.argv[1]); "
                "print(hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else '')"
            ),
            normalized_path,
        ],
        timeout=20,
    )
    if completed.returncode != 0:
        return {
            "status": "failed",
            "reason": "container_sha256_command_failed",
            "stdout_tail": str(completed.stdout or "")[-400:],
            "stderr_tail": str(completed.stderr or "")[-400:],
        }
    digest = str(completed.stdout or "").strip()
    if not digest:
        return {"status": "failed", "reason": "container_script_missing"}
    return {"status": "pass", "sha256": digest}


def _copy_path_to_container(container: str, local_path: Path, container_path: str) -> dict[str, object]:
    normalized_container = str(container or "").strip()
    normalized_path = str(container_path or "").strip()
    if not normalized_container:
        return {"status": "blocked", "reason": "container_missing"}
    if not local_path.is_file():
        return {"status": "blocked", "reason": "local_path_missing", "local_path": str(local_path)}
    if not normalized_path:
        return {"status": "blocked", "reason": "container_path_missing"}
    parent = str(Path(normalized_path).parent)
    mkdir = _run(["docker", "exec", normalized_container, "mkdir", "-p", parent], timeout=20)
    if mkdir.returncode != 0:
        return {
            "status": "failed",
            "reason": "container_parent_create_failed",
            "stdout_tail": str(mkdir.stdout or "")[-400:],
            "stderr_tail": str(mkdir.stderr or "")[-400:],
        }
    copied = _run(["docker", "cp", str(local_path), f"{normalized_container}:{normalized_path}"], timeout=60)
    if copied.returncode != 0:
        return {
            "status": "failed",
            "reason": "container_copy_failed",
            "stdout_tail": str(copied.stdout or "")[-400:],
            "stderr_tail": str(copied.stderr or "")[-400:],
        }
    return {"status": "pass", "local_path": str(local_path), "container_path": normalized_path}


def _sync_runtime_files_to_container(container: str, parity: dict[str, object]) -> dict[str, object]:
    mismatched_paths = {str(path or "").strip() for path in list(parity.get("mismatched_paths") or [])}
    copied: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for local_path, container_path in CRITICAL_RUNTIME_SCRIPT_PATHS:
        if container_path not in mismatched_paths:
            continue
        result = _copy_path_to_container(container, local_path, container_path)
        row = {"local_path": str(local_path), "container_path": container_path, **result}
        if result.get("status") == "pass":
            copied.append(row)
        else:
            failures.append(row)
    return {
        "status": "pass" if copied and not failures else ("skipped" if not copied and not failures else "failed"),
        "copied": copied,
        "failures": failures,
    }


def _runtime_code_parity(container: str) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    mismatches: list[str] = []
    for local_path, container_path in CRITICAL_RUNTIME_SCRIPT_PATHS:
        local_exists = local_path.is_file()
        local_sha256 = _sha256_path(local_path) if local_exists else ""
        container_digest = _container_path_sha256(container, container_path)
        container_sha256 = str(container_digest.get("sha256") or "").strip()
        match = bool(local_exists and container_digest.get("status") == "pass" and local_sha256 == container_sha256)
        if not match:
            mismatches.append(container_path)
        checks.append(
            {
                "local_path": str(local_path),
                "container_path": container_path,
                "local_exists": local_exists,
                "local_sha256": local_sha256,
                "container_status": str(container_digest.get("status") or ""),
                "container_reason": str(container_digest.get("reason") or ""),
                "container_sha256": container_sha256,
                "match": match,
            }
        )
    return {
        "status": "pass" if not mismatches else "failed",
        "checks": checks,
        "mismatched_paths": mismatches,
    }


def _container_ready(state: dict[str, object]) -> bool:
    return bool(state.get("exists")) and str(state.get("status") or "").strip() == "running"


def build_render_bridge_runtime_receipt(
    *,
    container: str = DEFAULT_RENDER_CONTAINER,
    service: str = DEFAULT_RENDER_SERVICE,
    compose_file: str = DEFAULT_COMPOSE_FILE,
    compose_project_name: str = "",
    health_url: str = DEFAULT_HEALTH_URL,
    timeout_seconds: int = 180,
) -> dict[str, object]:
    started_at = _now_iso()
    normalized_container = str(container or "").strip() or DEFAULT_RENDER_CONTAINER
    normalized_service = str(service or "").strip() or DEFAULT_RENDER_SERVICE
    normalized_compose_file = str(compose_file or "").strip() or DEFAULT_COMPOSE_FILE
    normalized_project_name = str(compose_project_name or "").strip()
    normalized_health_url = str(health_url or "").strip() or DEFAULT_HEALTH_URL
    bounded_timeout_seconds = max(30, int(timeout_seconds or 0))
    compose_path = Path(normalized_compose_file).expanduser()
    if not compose_path.is_absolute():
        compose_path = (ROOT / compose_path).resolve()

    receipt: dict[str, object] = {
        "contract_name": CONTRACT_NAME,
        "generated_at": started_at,
        "container": normalized_container,
        "service": normalized_service,
        "compose_file": str(compose_path),
        "compose_project_name": normalized_project_name,
        "health_url": normalized_health_url,
    }
    if not _docker_available():
        return {
            **receipt,
            "status": "blocked",
            "reason": "docker_missing",
        }

    if not compose_path.is_file():
        return {
            **receipt,
            "status": "blocked",
            "reason": "compose_file_missing",
        }

    initial_state = _container_state(normalized_container)
    receipt["initial_state"] = initial_state
    initial_code_parity: dict[str, object] = {"status": "skipped", "reason": "container_not_ready"}
    if _container_ready(initial_state):
        initial_probe = _container_health_probe(normalized_container, health_url=normalized_health_url)
        receipt["health_probe"] = initial_probe
        if initial_probe.get("status") == "pass":
            initial_code_parity = _runtime_code_parity(normalized_container)
            receipt["initial_code_parity"] = initial_code_parity
            if initial_code_parity.get("status") == "pass":
                return {
                    **receipt,
                    "status": "pass",
                    "action": "already_ready",
                    "post_state": initial_state,
                }
            sync = _sync_runtime_files_to_container(normalized_container, initial_code_parity)
            receipt["runtime_file_sync"] = sync
            if sync.get("status") == "pass":
                synced_code_parity = _runtime_code_parity(normalized_container)
                receipt["synced_code_parity"] = synced_code_parity
                if synced_code_parity.get("status") == "pass":
                    return {
                        **receipt,
                        "status": "pass",
                        "action": "synced_runtime_files",
                        "post_state": initial_state,
                        "post_code_parity": synced_code_parity,
                    }
        else:
            initial_code_parity = {"status": "skipped", "reason": "health_probe_failed"}
    receipt["initial_code_parity"] = initial_code_parity

    compose_command = _compose_command(
        compose_file=str(compose_path),
        compose_project_name=normalized_project_name,
    )
    if compose_command is None:
        return {
            **receipt,
            "status": "blocked",
            "reason": "docker_compose_missing",
        }

    refresh = _run(
        [
            *compose_command,
            "up",
            "-d",
            "--build",
            "--no-deps",
            "--force-recreate",
            normalized_service,
        ],
        timeout=max(300, min(max(bounded_timeout_seconds * 3, bounded_timeout_seconds), 1800)),
    )
    receipt["compose_up"] = {
        "returncode": int(refresh.returncode),
        "stdout_tail": str(refresh.stdout or "")[-1000:],
        "stderr_tail": str(refresh.stderr or "")[-1000:],
        "build_requested": True,
    }
    if refresh.returncode != 0:
        return {
            **receipt,
            "status": "failed",
            "reason": "render_bridge_compose_up_failed",
        }

    deadline = time.monotonic() + float(bounded_timeout_seconds)
    final_state = _container_state(normalized_container)
    final_probe = {"status": "blocked", "reason": "probe_not_run"}
    final_code_parity: dict[str, object] = {"status": "blocked", "reason": "probe_not_run"}
    while time.monotonic() < deadline:
        final_state = _container_state(normalized_container)
        if _container_ready(final_state):
            final_probe = _container_health_probe(normalized_container, health_url=normalized_health_url)
            if final_probe.get("status") == "pass":
                final_code_parity = _runtime_code_parity(normalized_container)
                if final_code_parity.get("status") == "pass":
                    return {
                        **receipt,
                        "status": "pass",
                        "action": "recreated_runtime",
                        "post_state": final_state,
                        "health_probe": final_probe,
                        "post_code_parity": final_code_parity,
                    }
        time.sleep(2)

    return {
        **receipt,
        "status": "failed",
        "reason": "render_bridge_runtime_not_ready",
        "post_state": final_state,
        "health_probe": final_probe,
        "post_code_parity": final_code_parity,
        "timeout_seconds": bounded_timeout_seconds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure the PropertyQuarry reconstruction render bridge runtime is running and healthy.")
    parser.add_argument("--container", default=os.getenv("PROPERTYQUARRY_RENDER_CONTAINER_NAME") or DEFAULT_RENDER_CONTAINER)
    parser.add_argument("--service", default=os.getenv("PROPERTYQUARRY_RENDER_SERVICE") or DEFAULT_RENDER_SERVICE)
    parser.add_argument("--compose-file", default=os.getenv("PROPERTYQUARRY_COMPOSE_FILE") or DEFAULT_COMPOSE_FILE)
    parser.add_argument("--project-name", default=os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or os.getenv("COMPOSE_PROJECT_NAME") or "")
    parser.add_argument("--health-url", default=os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_HEALTH_URL") or DEFAULT_HEALTH_URL)
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_READY_TIMEOUT_SECONDS") or "180"))
    parser.add_argument("--write", default="")
    args = parser.parse_args()

    receipt = build_render_bridge_runtime_receipt(
        container=args.container,
        service=args.service,
        compose_file=args.compose_file,
        compose_project_name=args.project_name,
        health_url=args.health_url,
        timeout_seconds=args.timeout_seconds,
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
