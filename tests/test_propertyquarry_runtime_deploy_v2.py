from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from scripts import propertyquarry_runtime_deploy_v2 as deploy


def _pem_private(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _pem_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _key_id(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return deploy._sha256_id(raw)


class Harness:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.runtime_sha = "1" * 40
        self.deployment_id = "2" * 64
        self.envelope_sha = "3" * 40
        self.web_image = (
            "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:"
            + "4" * 64
        )
        self.render_image = (
            "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:"
            + "5" * 64
        )
        self.cloudflared_image = "cloudflare/cloudflared@sha256:" + "6" * 64
        self.database_image = deploy.DATABASE_IMAGE
        self.database_repo_digest = deploy._canonical_repo_digest(
            self.database_image
        )
        self.database_image_id = "sha256:" + "7" * 64
        self.database_container_id = "8" * 64
        self.database_oid = 424242
        self.package_key = Ed25519PrivateKey.generate()
        self.receipt_key = Ed25519PrivateKey.generate()
        self.package_key_id = _key_id(self.package_key)
        self.receipt_key_id = _key_id(self.receipt_key)
        self.machine_id = "9" * 32
        self.machine_digest = deploy._sha256_id(self.machine_id.encode("ascii"))
        self.now = int(time.time())

        self.install = root / "etc/propertyquarry-release-single-host-v2"
        self.property_root = root / "docker/property"
        self.docker = root / "usr/bin/docker"
        self.plugin = root / "usr/libexec/docker/cli-plugins/docker-compose"
        self.compose_files = (
            self.property_root / "docker-compose.property.yml",
            self.property_root / "docker-compose.cloudflared.yml",
        )
        self.env_files = (
            self.property_root / ".env",
            self.property_root / "state/runtime/property_scene_video_shared.env",
            self.property_root / "state/runtime/propertyquarry_database_roles.env",
            self.property_root / "state/runtime/propertyquarry_admission.env",
            self.property_root / "state/runtime/propertyquarry_google_identity.env",
            self.property_root / "state/runtime/propertyquarry_registration_email.env",
        )
        self.deploy_root = root / "var/deploy-receipts"
        self.backup_root = root / "var/backup-receipts"
        self.isolation_root = root / "var/isolation-receipts"
        self.database_root = root / "var/database-receipts"
        self.machine_path = root / "etc/machine-id"

        patches = {
            "ROOT_UID": self.uid,
            "ROOT_GID": self.gid,
            "RUNTIME_UID": self.uid,
            "RUNTIME_GID": self.gid,
            "INSTALL_ROOT": self.install,
            "AUTHORITY_PATH": self.install / "authority.v2.json",
            "AUTHORITY_SIGNATURE_PATH": self.install / "authority.v2.sig",
            "PLAN_PATH": self.install / "transaction-plan.v2.json",
            "MANIFEST_PATH": self.install / "package-manifest.v2.json",
            "MANIFEST_SIGNATURE_PATH": self.install / "package-manifest.v2.sig",
            "PACKAGE_PUBLIC_KEY_PATH": self.install / "package-authority-v2.pem",
            "RECEIPT_PRIVATE_KEY_PATH": self.install / "receipt-authority-v2.key",
            "RECEIPT_PUBLIC_KEY_PATH": self.install / "receipt-authority-v2.pem",
            "MACHINE_ID_PATH": self.machine_path,
            "PROPERTY_ROOT": self.property_root,
            "DOCKER_EXECUTABLE": self.docker,
            "COMPOSE_PLUGIN": self.plugin,
            "COMPOSE_FILES": self.compose_files,
            "ENV_FILES": self.env_files,
            "DATABASE_ENV": self.env_files[2],
            "DEPLOY_RECEIPT_ROOT": self.deploy_root,
            "BACKUP_RECEIPT_ROOT": self.backup_root,
            "ISOLATION_RECEIPT_ROOT": self.isolation_root,
            "DATABASE_RECEIPT_ROOT": self.database_root,
        }
        for name, value in patches.items():
            monkeypatch.setattr(deploy, name, value)
        monkeypatch.setattr(deploy.os, "geteuid", lambda: 0)

        self._write(self.docker, b"fixture-docker\n", 0o755)
        self._write(self.plugin, b"fixture-compose-plugin\n", 0o755)
        self._write(self.compose_files[0], b"services: {api: {}}\n", 0o644)
        self._write(self.compose_files[1], b"services: {tunnel: {}}\n", 0o644)
        for index, path in enumerate(self.env_files):
            self._write(path, f"FIXTURE_{index}=value-{index}\n".encode(), 0o600)
        self._write(self.machine_path, (self.machine_id + "\n").encode(), 0o444)
        self.deploy_root.mkdir(parents=True, mode=0o700)
        os.chmod(self.deploy_root, 0o700)
        self._write(
            self.install / "package-authority-v2.pem",
            _pem_public(self.package_key),
            0o444,
        )
        self._write(
            self.install / "receipt-authority-v2.key",
            _pem_private(self.receipt_key),
            0o400,
        )
        self._write(
            self.install / "receipt-authority-v2.pem",
            _pem_public(self.receipt_key),
            0o444,
        )
        self.pgdata = {
            "created_at": "2026-07-22T00:00:00Z",
            "driver": "local",
            "labels": {
                "com.docker.compose.project": "property",
                "com.docker.compose.volume": "propertyquarry_pgdata",
            },
            "mountpoint": "/var/lib/docker/volumes/property_propertyquarry_pgdata/_data",
            "name": "property_propertyquarry_pgdata",
            "options": {},
            "scope": "local",
        }
        self._build_documents_and_receipts()
        monkeypatch.setattr(
            deploy,
            "_measure_database_substrate",
            lambda **_kwargs: dict(self.substrate),
        )

    @staticmethod
    def _write(path: Path, raw: bytes, mode: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(mode)
        return path

    @property
    def receipt_path(self) -> Path:
        return (
            self.deploy_root
            / self.runtime_sha
            / self.deployment_id
            / "deploy-runtime.json"
        )

    @property
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            operation="deploy-runtime",
            runtime_sha=self.runtime_sha,
            deployment_id=self.deployment_id,
            envelope_sha=self.envelope_sha,
            web_image=self.web_image,
            render_image=self.render_image,
            cloudflared_image=self.cloudflared_image,
            database_image=self.database_image,
            api_host_ip=deploy.API_HOST_IP,
            api_host_port=deploy.API_HOST_PORT,
            api_container_port=deploy.API_CONTAINER_PORT,
            receipt=str(self.receipt_path),
        )

    def _observation(self, path: Path) -> dict[str, object]:
        raw = path.read_bytes()
        metadata = path.stat()
        return {
            "gid": metadata.st_gid,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "path": str(path),
            "sha256": deploy._sha256_id(raw),
            "size": len(raw),
            "uid": metadata.st_uid,
        }

    def _sign_wrapper(self, payload: dict[str, object], domain: bytes) -> bytes:
        encoded = deploy._canonical_json(payload)
        signature = self.receipt_key.sign(deploy._framed(domain, encoded))
        wrapper = {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .decode("ascii")
            .rstrip("="),
            "signature_key_id": self.receipt_key_id,
        }
        return deploy._canonical_json(wrapper) + b"\n"

    def _receipt(
        self,
        path: Path,
        payload: dict[str, object],
        domain: bytes,
    ) -> str:
        raw = self._sign_wrapper(payload, domain)
        self._write(path, raw, 0o600)
        return deploy._sha256_id(raw)

    def _build_documents_and_receipts(self) -> None:
        post_inputs = [self._observation(path) for path in self.env_files]
        pre_inputs = [dict(item) for item in post_inputs]
        pre_inputs[0] = dict(pre_inputs[0])
        pre_inputs[0]["sha256"] = "sha256:" + "a" * 64
        pre_inputs[0]["size"] = int(pre_inputs[0]["size"]) + 17
        runtime_deploy = {
            "compose_argv": deploy._expected_compose_argv(),
            "compose_files": [self._observation(path) for path in self.compose_files],
            "compose_plugin": self._observation(self.plugin),
            "deployment_id": self.deployment_id,
            "docker_executable": self._observation(self.docker),
            "env_files": [str(path) for path in self.env_files],
            "operation": "deploy-runtime",
            "receipt_path": str(self.receipt_path),
        }
        runtime_retirement = {
            "containers": [],
            "deployment_id": self.deployment_id,
            "desired_live_allowlist": [
                "propertyquarry-api-live",
                "propertyquarry-cloudflared-live",
                "propertyquarry-db-live",
                "propertyquarry-render-live",
                "propertyquarry-scheduler-live",
                "propertyquarry-worker-live",
            ],
            "operation": "retire-stale-propertyquarry-runtime",
            "preserve_volumes": True,
            "receipt_path": str(
                self.isolation_root
                / self.runtime_sha
                / self.deployment_id
                / "retire-stale-propertyquarry-runtime.json"
            ),
        }
        retirement_digest = deploy._sha256_id(
            deploy._canonical_json(runtime_retirement)
        )
        database_substrate = {
            "container_id": self.database_container_id,
            "container_name": "propertyquarry-db-live",
            "database": "propertyquarry",
            "database_oid": self.database_oid,
            "image": self.database_image,
            "image_id": self.database_image_id,
            "pgdata_volume": self.pgdata,
            "repo_digest": self.database_repo_digest,
        }
        self.substrate = database_substrate
        common = {
            "api_container_port": deploy.API_CONTAINER_PORT,
            "api_host_ip": deploy.API_HOST_IP,
            "api_host_port": deploy.API_HOST_PORT,
            "backup_max_age_seconds": 3600,
            "cloudflared_image": self.cloudflared_image,
            "database_substrate": database_substrate,
            "database_image": self.database_image,
            "deployment_id": self.deployment_id,
            "envelope_sha": self.envelope_sha,
            "host_machine_id_digest": self.machine_digest,
            "post_purge_root_env_digest": post_inputs[0]["sha256"],
            "pre_purge_root_env_digest": pre_inputs[0]["sha256"],
            "pre_purge_runtime_inputs": pre_inputs,
            "render_image": self.render_image,
            "runtime_deploy": runtime_deploy,
            "runtime_inputs": post_inputs,
            "runtime_retirement": runtime_retirement,
            "runtime_retirement_digest": retirement_digest,
            "runtime_sha": self.runtime_sha,
            "transaction_started_at_epoch": self.now - 110,
            "scene_video_env_digest": post_inputs[1]["sha256"],
            "scene_video_env_gid": self.gid,
            "scene_video_env_uid": self.uid,
            "github_identity_env_digest": post_inputs[4]["sha256"],
            "github_identity_env_gid": self.gid,
            "github_identity_env_uid": self.uid,
            "registration_email_env_digest": post_inputs[5]["sha256"],
            "registration_email_env_gid": self.gid,
            "registration_email_env_uid": self.uid,
            "web_image": self.web_image,
        }
        plan = {**common, "schema": deploy.PLAN_SCHEMA}
        plan_raw = deploy._canonical_json(plan)
        authority = {
            **common,
            "package_authority_key_id": self.package_key_id,
            "plan_digest": deploy._sha256_id(plan_raw),
            "receipt_authority_key_id": self.receipt_key_id,
            "schema": deploy.AUTHORITY_SCHEMA,
        }
        authority_raw = deploy._canonical_json(authority)
        authority_signature = self.package_key.sign(
            deploy._framed(deploy.AUTHORITY_SIGNATURE_DOMAIN, authority_raw)
        )
        manifest = {
            "api_container_port": deploy.API_CONTAINER_PORT,
            "api_host_ip": deploy.API_HOST_IP,
            "api_host_port": deploy.API_HOST_PORT,
            "cloudflared_image": self.cloudflared_image,
            "config_digest": deploy._sha256_id(authority_raw),
            "database_image": self.database_image,
            "deployment_id": self.deployment_id,
            "envelope_sha": self.envelope_sha,
            "package_authority_key_id": self.package_key_id,
            "plan_digest": deploy._sha256_id(plan_raw),
            "receipt_authority_key_id": self.receipt_key_id,
            "runtime_sha": self.runtime_sha,
            "schema": deploy.MANIFEST_SCHEMA,
        }
        manifest_raw = deploy._canonical_json(manifest)
        manifest_signature = self.package_key.sign(
            deploy._framed(deploy.MANIFEST_SIGNATURE_DOMAIN, manifest_raw)
        )
        self._write(self.install / "authority.v2.json", authority_raw, 0o400)
        self._write(self.install / "authority.v2.sig", authority_signature, 0o444)
        self._write(self.install / "transaction-plan.v2.json", plan_raw, 0o444)
        self._write(self.install / "package-manifest.v2.json", manifest_raw, 0o444)
        self._write(
            self.install / "package-manifest.v2.sig", manifest_signature, 0o444
        )
        self.authority = authority
        self.runtime_inputs = post_inputs
        self.runtime_deploy = runtime_deploy
        self.runtime_retirement = runtime_retirement
        self.retirement_contract_digest = retirement_digest
        self.bindings = {
            "authority_digest": deploy._sha256_id(authority_raw),
            "authority_signature_digest": deploy._sha256_id(authority_signature),
            "config_digest": deploy._sha256_id(authority_raw),
            "package_authority_key_id": self.package_key_id,
            "package_manifest_digest": deploy._sha256_id(manifest_raw),
            "package_manifest_signature_digest": deploy._sha256_id(
                manifest_signature
            ),
            "plan_digest": deploy._sha256_id(plan_raw),
        }

        backup_payload = {
            **self.bindings,
            "backup_max_age_seconds": 3600,
            "database_image": self.database_image,
            "database_image_id": self.database_image_id,
            "database_repo_digest": self.database_repo_digest,
            "database_substrate_after": database_substrate,
            "database_substrate_before": database_substrate,
            "deployment_id": self.deployment_id,
            "disposition": "verified-and-published",
            "envelope_sha": self.envelope_sha,
            "finished_at_epoch": self.now - 90,
            "host_machine_id_digest": self.machine_digest,
            "plaintext_retained": False,
            "production_ready": False,
            "pre_purge_runtime_inputs": pre_inputs,
            "receipt_authority_key_id": self.receipt_key_id,
            "render_image": self.render_image,
            "runtime_sha": self.runtime_sha,
            "schema": deploy.BACKUP_SCHEMA,
            "started_at_epoch": self.now - 100,
            "transaction_started_at_epoch": self.now - 110,
            "web_image": self.web_image,
        }
        backup_path = (
            self.backup_root
            / self.runtime_sha
            / self.deployment_id
            / "create.json"
        )
        self.backup_digest = self._receipt(
            backup_path, backup_payload, deploy.BACKUP_SIGNATURE_DOMAIN
        )

        isolation_common = {
            **self.bindings,
            "api_container_port": deploy.API_CONTAINER_PORT,
            "api_host_ip": deploy.API_HOST_IP,
            "api_host_port": deploy.API_HOST_PORT,
            "backup_max_age_seconds": 3600,
            "cloudflared_image": self.cloudflared_image,
            "database_image": self.database_image,
            "database_substrate": database_substrate,
            "database_substrate_digest": deploy._sha256_id(
                deploy._canonical_json(database_substrate)
            ),
            "deployment_id": self.deployment_id,
            "envelope_sha": self.envelope_sha,
            "host_machine_id_digest": self.machine_digest,
            "post_purge_root_env_digest": post_inputs[0]["sha256"],
            "pre_purge_root_env_digest": pre_inputs[0]["sha256"],
            "pre_purge_runtime_inputs": pre_inputs,
            "production_ready": False,
            "receipt_authority_key_id": self.receipt_key_id,
            "render_image": self.render_image,
            "runtime_deploy_digest": deploy._sha256_id(
                deploy._canonical_json(runtime_deploy)
            ),
            "runtime_inputs": post_inputs,
            "runtime_retirement_digest": retirement_digest,
            "runtime_sha": self.runtime_sha,
            "schema": deploy.ISOLATION_SCHEMA,
            "secret_values_emitted": False,
            "status": "verified",
            "transaction_started_at_epoch": self.now - 110,
            "web_image": self.web_image,
        }
        purge_payload = {
            **isolation_common,
            "finished_at_epoch": self.now - 70,
            "operation": "purge-legacy-runtime-exposure",
            "result": {
                "backup_receipt_sha256": self.backup_digest,
                "inputs": {
                    "file_digests": {
                        str(item["path"]): str(item["sha256"])
                        for item in post_inputs
                    },
                    "google_key_count": 5,
                    "legacy_registration_email_present": False,
                    "registration_email_key_count": 8,
                },
                "legacy_keys_removed": 8,
                "post_purge_root_env_digest": post_inputs[0]["sha256"],
                "pre_purge_root_env_digest": pre_inputs[0]["sha256"],
                "rollback_artifact": {"ciphertext_sha256": "sha256:" + "b" * 64},
                "rollback_artifact_expected_removed_keys": 8,
            },
            "started_at_epoch": self.now - 80,
        }
        isolation_directory = (
            self.isolation_root / self.runtime_sha / self.deployment_id
        )
        self.purge_path = isolation_directory / "purge-legacy-runtime-exposure.json"
        self.purge_digest = self._receipt(
            self.purge_path, purge_payload, deploy.ISOLATION_SIGNATURE_DOMAIN
        )
        retirement_payload = {
            **isolation_common,
            "finished_at_epoch": self.now - 60,
            "operation": "retire-stale-propertyquarry-runtime",
            "result": {
                "backup_receipt_sha256": self.backup_digest,
                "purge_receipt_sha256": self.purge_digest,
                "preserved_volumes": [self.pgdata],
                "retired_containers": [],
                "unknown_matches": [],
                "volumes_removed": False,
            },
            "started_at_epoch": self.now - 69,
        }
        self.retirement_path = (
            isolation_directory / "retire-stale-propertyquarry-runtime.json"
        )
        self.retirement_digest = self._receipt(
            self.retirement_path,
            retirement_payload,
            deploy.ISOLATION_SIGNATURE_DOMAIN,
        )

        self.database_paths: dict[str, Path] = {}
        db_directory = self.database_root / self.runtime_sha / self.deployment_id
        env_digest = str(post_inputs[2]["sha256"])
        predecessor_digest = self.retirement_digest
        for index, operation in enumerate(deploy.DATABASE_OPERATIONS):
            if operation == "provision-roles":
                result: dict[str, object] = {
                    "credential_reused": False,
                    "database_oid": self.database_oid,
                    "roles": list(deploy.DATABASE_ROLES),
                }
            else:
                readiness = operation != "migrate-schema"
                schema: dict[str, object] = {
                    "status": "ready" if readiness else "migrated"
                }
                if readiness:
                    schema["ready"] = True
                for field, component in (
                    ("google_identity", "propertyquarry_google_identity"),
                    ("kernel", "ea_kernel"),
                    ("property_search", "property_search"),
                ):
                    schema[field] = (
                        {
                            "applied_versions": [1],
                            "component": component,
                            "current_version": 1,
                            "ready": True,
                            "reason": "ready",
                            "required_version": 1,
                        }
                        if readiness
                        else {
                            "applied_versions": [1],
                            "component": component,
                            "current_version": 1,
                            "previous_version": 0,
                        }
                    )
                result = {
                    "credential_reused": True,
                    "database_oid": self.database_oid,
                    "schema": schema,
                }
            payload = {
                "authority_digest": self.bindings["authority_digest"],
                "backup_max_age_seconds": 3600,
                "backup_receipt_sha256": self.backup_digest,
                "database": "propertyquarry",
                "database_container": "propertyquarry-db-live",
                "database_image": self.database_image,
                "database_image_id": self.database_image_id,
                "database_repo_digest": self.database_repo_digest,
                "database_substrate_after": database_substrate,
                "database_substrate_before": database_substrate,
                "deployment_id": self.deployment_id,
                "docker_network": "property_default",
                "env_file": str(self.env_files[2]),
                "env_file_sha256": env_digest,
                "finished_at_epoch": self.now - 48 + index * 10,
                "host_machine_id_digest": self.machine_digest,
                "operation": operation,
                "predecessor_receipt_sha256": predecessor_digest,
                "production_ready": False,
                "purge_receipt_sha256": self.purge_digest,
                "receipt_authority_key_id": self.receipt_key_id,
                "result": result,
                "retirement_receipt_sha256": self.retirement_digest,
                "runtime_inputs": post_inputs,
                "runtime_sha": self.runtime_sha,
                "schema": deploy.DATABASE_SCHEMA,
                "secret_values_emitted": False,
                "started_at_epoch": self.now - 58 + index * 10,
                "status": "verified",
                "transaction_started_at_epoch": self.now - 110,
                "web_image": self.web_image,
            }
            path = db_directory / f"{operation}.json"
            predecessor_digest = self._receipt(
                path, payload, deploy.DATABASE_SIGNATURE_DOMAIN
            )
            self.database_paths[operation] = path

    def resign_receipt(
        self,
        path: Path,
        domain: bytes,
        mutate,
    ) -> None:
        wrapper = json.loads(path.read_bytes())
        payload = wrapper["payload"]
        mutate(payload)
        self._write(path, self._sign_wrapper(payload, domain), 0o600)

    def run(self) -> dict[str, object]:
        secret_stdout = b"super-secret-compose-output"
        secret_stderr = b"another-secret"
        expected_argv = list(self.runtime_deploy["compose_argv"])

        def fake_run(argv, docker_observation):
            assert list(argv) == expected_argv
            assert docker_observation == self.runtime_deploy["docker_executable"]
            return deploy.ProcessResult(
                exit_code=0,
                stdout_bytes=len(secret_stdout),
                stdout_sha256=deploy._sha256_id(secret_stdout),
                stderr_bytes=len(secret_stderr),
                stderr_sha256=deploy._sha256_id(secret_stderr),
            )

        self.monkeypatch.setattr(deploy, "_run_compose", fake_run)
        return deploy.execute_signed(self.args)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def test_production_compose_argv_is_exact() -> None:
    expected = [
        "/usr/bin/docker",
        "compose",
        "--ansi",
        "never",
        "--progress",
        "quiet",
        "--project-name",
        "property",
        "--project-directory",
        "/docker/property",
    ]
    for path in (
        "/docker/property/.env",
        "/docker/property/state/runtime/property_scene_video_shared.env",
        "/docker/property/state/runtime/propertyquarry_database_roles.env",
        "/docker/property/state/runtime/propertyquarry_admission.env",
        "/docker/property/state/runtime/propertyquarry_google_identity.env",
        "/docker/property/state/runtime/propertyquarry_registration_email.env",
    ):
        expected.extend(("--env-file", path))
    expected.extend(
        (
            "--file",
            "/docker/property/docker-compose.property.yml",
            "--file",
            "/docker/property/docker-compose.cloudflared.yml",
            "up",
            "--detach",
            "--pull",
            "always",
            "--quiet-pull",
            "--no-build",
            "--remove-orphans",
            "--timeout",
            "120",
            "--wait",
            "--wait-timeout",
            "900",
        )
    )
    assert deploy._expected_compose_argv() == expected
    assert deploy.SUBPROCESS_TIMEOUT_SECONDS == 1800


def test_signed_deploy_receipt_binds_every_predecessor_and_redacts_output(
    harness: Harness,
) -> None:
    wrapper = harness.run()
    payload = wrapper["payload"]
    assert payload["schema"] == deploy.SCHEMA
    assert payload["deployment_id"] == harness.deployment_id
    assert payload["backup_receipt_sha256"] == harness.backup_digest
    assert payload["purge_receipt_sha256"] == harness.purge_digest
    assert payload["retirement_receipt_sha256"] == harness.retirement_digest
    assert list(payload["database_receipts"]) == list(deploy.DATABASE_OPERATIONS)
    assert payload["database_container_id"] == harness.database_container_id
    assert payload["database_pgdata_volume"] == harness.pgdata
    assert payload["runtime_inputs"] == harness.runtime_inputs
    assert payload["pre_observations"] == payload["post_observations"]
    assert payload["pull_policy"] == "always"
    assert payload["build_performed"] is False
    assert payload["orphans_removed"] is True
    assert payload["wait_completed"] is True
    assert payload["output_redacted"] is True
    assert payload["secret_values_emitted"] is False
    raw = harness.receipt_path.read_bytes()
    assert raw == deploy._canonical_json(json.loads(raw)) + b"\n"
    assert b"super-secret-compose-output" not in raw
    assert b"another-secret" not in raw
    assert stat.S_IMODE(harness.receipt_path.stat().st_mode) == 0o600


def test_rejects_wrong_deployment_namespace(harness: Harness) -> None:
    args = harness.args
    args.deployment_id = "f" * 64
    args.receipt = str(
        harness.deploy_root
        / args.runtime_sha
        / args.deployment_id
        / "deploy-runtime.json"
    )
    with pytest.raises(deploy.DeployError, match="installed_authority_binding_invalid"):
        deploy.execute_signed(args)


def test_rejects_compose_file_substitution_before_execution(harness: Harness) -> None:
    harness.compose_files[0].write_text("services: {attacker: {}}\n")
    with pytest.raises(deploy.DeployError, match="pre_observation_binding_invalid"):
        harness.run()
    assert not harness.receipt_path.exists()


def test_rejects_runtime_input_substitution(harness: Harness) -> None:
    harness.env_files[3].write_text("ATTACKER_SECRET=changed\n")
    harness.env_files[3].chmod(0o600)
    with pytest.raises(deploy.DeployError, match="runtime_inputs_pre_binding_invalid"):
        harness.run()
    assert not harness.receipt_path.exists()


def test_rejects_post_execution_compose_mutation(harness: Harness) -> None:
    def mutating_run(_argv, _docker_observation):
        harness.compose_files[1].write_text("services: {changed: {}}\n")
        return deploy.ProcessResult(
            exit_code=0,
            stdout_bytes=0,
            stdout_sha256=deploy._sha256_id(b""),
            stderr_bytes=0,
            stderr_sha256=deploy._sha256_id(b""),
        )

    harness.monkeypatch.setattr(deploy, "_run_compose", mutating_run)
    with pytest.raises(deploy.DeployError, match="post_observation_binding_invalid"):
        deploy.execute_signed(harness.args)
    assert not harness.receipt_path.exists()


def test_rejects_database_substrate_replacement_during_compose(
    harness: Harness,
) -> None:
    observations = [dict(harness.substrate), dict(harness.substrate)]
    observations[1]["container_id"] = "f" * 64

    def measure(**_kwargs):
        return observations.pop(0)

    harness.monkeypatch.setattr(deploy, "_measure_database_substrate", measure)
    harness.monkeypatch.setattr(
        deploy,
        "_run_compose",
        lambda _argv, _docker: deploy.ProcessResult(
            exit_code=0,
            stdout_bytes=0,
            stdout_sha256=deploy._sha256_id(b""),
            stderr_bytes=0,
            stderr_sha256=deploy._sha256_id(b""),
        ),
    )
    with pytest.raises(
        deploy.DeployError, match="database_substrate_post_binding_invalid"
    ):
        deploy.execute_signed(harness.args)
    assert not harness.receipt_path.exists()


def test_rejects_forged_purge_receipt(harness: Harness) -> None:
    wrapper = json.loads(harness.purge_path.read_bytes())
    signature = wrapper["signature"]
    wrapper["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    harness._write(
        harness.purge_path, deploy._canonical_json(wrapper) + b"\n", 0o600
    )
    with pytest.raises(deploy.DeployError, match="signature_invalid"):
        harness.run()


def test_rejects_resigned_purge_input_summary_substitution(
    harness: Harness,
) -> None:
    harness.resign_receipt(
        harness.purge_path,
        deploy.ISOLATION_SIGNATURE_DOMAIN,
        lambda payload: payload["result"]["inputs"].__setitem__(
            "google_key_count", 4
        ),
    )
    with pytest.raises(
        deploy.DeployError, match="purge_receipt_environment_invalid"
    ):
        harness.run()


def test_rejects_resigned_retirement_unknown_match(harness: Harness) -> None:
    harness.resign_receipt(
        harness.retirement_path,
        deploy.ISOLATION_SIGNATURE_DOMAIN,
        lambda payload: payload["result"]["unknown_matches"].append("legacy"),
    )
    with pytest.raises(
        deploy.DeployError, match="retirement_receipt_result_binding_invalid"
    ):
        harness.run()


def test_rejects_resigned_retirement_purge_substitution(harness: Harness) -> None:
    harness.resign_receipt(
        harness.retirement_path,
        deploy.ISOLATION_SIGNATURE_DOMAIN,
        lambda payload: payload["result"].__setitem__(
            "purge_receipt_sha256", "sha256:" + "f" * 64
        ),
    )
    with pytest.raises(
        deploy.DeployError, match="retirement_receipt_result_binding_invalid"
    ):
        harness.run()


def test_rejects_resigned_database_container_substitution(harness: Harness) -> None:
    path = harness.database_paths["migrate-schema"]
    harness.resign_receipt(
        path,
        deploy.DATABASE_SIGNATURE_DOMAIN,
        lambda payload: payload["database_substrate_after"].__setitem__(
            "container_id", "f" * 64
        ),
    )
    with pytest.raises(deploy.DeployError, match="database_receipt_binding_invalid"):
        harness.run()


def test_rejects_database_receipt_reordering_by_time(harness: Harness) -> None:
    path = harness.database_paths["verify-schema-readiness"]

    def rewind(payload: dict[str, object]) -> None:
        payload["started_at_epoch"] = harness.now - 1000
        payload["finished_at_epoch"] = harness.now - 999

    harness.resign_receipt(path, deploy.DATABASE_SIGNATURE_DOMAIN, rewind)
    with pytest.raises(deploy.DeployError, match="receipt_time_invalid"):
        harness.run()


def test_rejects_signed_plan_byte_change(harness: Harness) -> None:
    plan_path = harness.install / "transaction-plan.v2.json"
    plan = json.loads(plan_path.read_bytes())
    plan["runtime_deploy"]["compose_argv"][-1] = "899"
    plan_path.chmod(0o600)
    harness._write(plan_path, deploy._canonical_json(plan), 0o444)
    with pytest.raises(deploy.DeployError, match="installed_digest_binding_invalid"):
        harness.run()
