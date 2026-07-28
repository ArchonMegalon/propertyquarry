from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import propertyquarry_local_deployment_receipt as receipt


COMMIT = "a" * 40
WEB = "sha256:" + ("b" * 64)
RENDER = "sha256:" + ("c" * 64)


def _inspect(service: str) -> dict[str, object]:
    completed = service == "propertyquarry-migrate"
    healthy = service in receipt.HEALTHY_SERVICES
    release_environment = [
        f"PROPERTYQUARRY_RELEASE_COMMIT_SHA={COMMIT}",
        f"PROPERTYQUARRY_RELEASE_IMAGE_DIGEST={WEB}",
        "PROPERTYQUARRY_RELEASE_REPOSITORY=ArchonMegalon/propertyquarry",
        "PROPERTYQUARRY_RELEASE_BRANCH=main",
        "PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID=propertyquarry-local-docker-a",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET=must-never-be-recorded",
        "DATABASE_URL=must-never-be-recorded",
    ]
    config = {
        "User": (
            "10001:10001"
            if service in receipt.RELEASE_BOUND_SERVICES
            else ""
        ),
        "Env": (
            release_environment
            if service in receipt.RELEASE_BOUND_SERVICES
            else []
        ),
        "Labels": {
            "com.docker.compose.project": "property",
            "com.docker.compose.service": service,
        },
    }
    state: dict[str, object] = {
        "Status": "exited" if completed else "running",
        "ExitCode": 0,
    }
    if healthy:
        state["Health"] = {"Status": "healthy"}
    return {
        "Id": service + "-container",
        "Name": "/" + service,
        "Image": (
            WEB
            if service in receipt.WEB_SERVICES
            else RENDER
            if service == "propertyquarry-render-tools"
            else "sha256:" + ("d" * 64)
        ),
        "Config": config,
        "HostConfig": {
            "Privileged": False,
            "SecurityOpt": (
                []
                if service == "propertyquarry-db"
                else ["no-new-privileges:true"]
            ),
        },
        "State": state,
        "Mounts": [],
    }


def _patch_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(receipt, "_git", lambda *_args: "e" * 40)
    monkeypatch.setattr(receipt, "_sha256", lambda _path: "sha256:" + ("f" * 64))
    monkeypatch.setattr(
        receipt,
        "_container_ids",
        lambda _project, service: [service],
    )
    monkeypatch.setattr(receipt, "_inspect", _inspect)
    monkeypatch.setattr(
        receipt,
        "_probe",
        lambda origin, **_kwargs: {
            "status": "pass",
            "http_status": 200,
            "target": origin + "/health/ready",
            "version_http_status": 200,
            "version_target": origin + "/version",
            "release_identity": {
                "release_commit_sha": COMMIT,
                "release_image_digest": WEB,
                "release_manifest_status": "complete",
                "release_manifest_sha256": "f" * 64,
            },
        },
    )
    monkeypatch.setattr(
        receipt,
        "_public_tour_volume_privacy",
        lambda _container_name: {
            "status": "pass",
            "mode": "audit",
            "counts": {
                "bundles": 1,
                "private_key_manifests": 0,
                "private_mode_violations": 0,
            },
            "secret_values_recorded": False,
        },
    )
    monkeypatch.setattr(
        receipt.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )


def test_local_docker_receipt_binds_all_services_without_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_happy_path(monkeypatch)

    result = receipt.audit_local_deployment(
        root=tmp_path,
        expected_commit=COMMIT,
        expected_web_image=WEB,
        expected_render_image=RENDER,
    )

    assert result["passed"] is True
    assert result["authority"] == {
        "scope": "local_docker",
        "proof_plane": "local_docker_operator_receipts",
        "github_actions_used": False,
        "canonical_repository": "ArchonMegalon/propertyquarry",
    }
    assert set(result["services"]) == set(receipt.SERVICE_CONTRACT)
    assert result["public_tour_volume_privacy"]["status"] == "pass"
    assert result["secret_values_recorded"] is False
    encoded = json.dumps(result)
    assert "must-never-be-recorded" not in encoded
    assert "DATABASE_URL" not in encoded
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in encoded


def test_local_docker_receipt_fails_closed_on_image_drift(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _patch_happy_path(monkeypatch)
    original = receipt._inspect

    def drift(service: str) -> dict[str, object]:
        value = original(service)
        if service == "propertyquarry-api":
            value["Image"] = "sha256:" + ("9" * 64)
        return value

    monkeypatch.setattr(receipt, "_inspect", drift)

    result = receipt.audit_local_deployment(
        root=tmp_path,
        expected_commit=COMMIT,
        expected_web_image=WEB,
        expected_render_image=RENDER,
    )

    assert result["passed"] is False
    assert "propertyquarry-api:web_image_mismatch" in result["failures"]
