from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import os
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = MODULE_ROOT.parents[1]
RUNNER_LIFECYCLE = (
    MODULE_ROOT / "tools/run-ephemeral-runner-with-docker.sh"
)
# A historical, non-authoritative fixture commit containing the exact helper
# bytes. Production runtime/workflow SHAs are JIT inputs after source merge.
FIXTURE_HELPER_COMMIT = "f20476af1899b8c2fac71e31c328c57da6450963"
RUNTIME_DATABASE_HELPER_GIT_BLOB = "a499ddf2e9129c8da73f7b37dc03ece880ea124f"
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_single_host_package", MODULE_ROOT / "tools" / "package.py"
)
assert SPEC is not None and SPEC.loader is not None
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-package-test-"
        )
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.mutation_index = 0
        self.package_private = Ed25519PrivateKey.generate()
        self.receipt_private = Ed25519PrivateKey.generate()
        self.paths = self._fixture_files()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _public_pem(key: Ed25519PrivateKey) -> bytes:
        return key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @staticmethod
    def _private_pem(key: Ed25519PrivateKey) -> bytes:
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def _write(self, name: str, raw: bytes, mode: int = 0o600) -> Path:
        path = self.root / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.write(descriptor, raw)
        finally:
            os.close(descriptor)
        os.chmod(path, mode)
        return path

    @staticmethod
    def _synthetic_static_elf() -> bytes:
        ident = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
        header = struct.pack(
            "<HHIQQQIHHHHHH",
            2,
            62,
            1,
            0x400000,
            64,
            0,
            0,
            64,
            56,
            2,
            0,
            0,
            0,
        )
        load = struct.pack(
            "<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, 176, 176, 4096
        )
        stack = struct.pack("<IIQQQQQQ", 0x6474E551, 6, 0, 0, 0, 0, 0, 16)
        return ident + header + load + stack

    @staticmethod
    def _sealed_helper(relative: str) -> bytes:
        source = REPOSITORY_ROOT / relative
        if source.is_file():
            raw = source.read_bytes()
            expected = {
                "scripts/propertyquarry_predeploy_backup_v2.py": package.PREDEPLOY_BACKUP_HELPER_SHA256,
                "scripts/propertyquarry_database_control_v2.py": package.DATABASE_CONTROL_HELPER_SHA256,
                "scripts/provision_propertyquarry_runtime_database.py": package.RUNTIME_DATABASE_HELPER_SHA256,
                "scripts/propertyquarry_runtime_isolation_v2.py": package.RUNTIME_ISOLATION_HELPER_SHA256,
                "scripts/propertyquarry_runtime_deploy_v2.py": package.RUNTIME_DEPLOY_HELPER_SHA256,
            }[relative]
            if package.sha256(raw) == expected:
                return raw
        completed = subprocess.run(
            ["git", "show", f"{FIXTURE_HELPER_COMMIT}:{relative}"],
            cwd=REPOSITORY_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.decode("utf-8", "replace"))
        return completed.stdout

    def test_sealed_helper_constants_match_current_repository_bytes(self) -> None:
        expected = (
            (
                "scripts/propertyquarry_predeploy_backup_v2.py",
                package.PREDEPLOY_BACKUP_HELPER_SHA256,
                package.PREDEPLOY_BACKUP_HELPER_BYTES,
            ),
            (
                "scripts/propertyquarry_database_control_v2.py",
                package.DATABASE_CONTROL_HELPER_SHA256,
                package.DATABASE_CONTROL_HELPER_BYTES,
            ),
            (
                "scripts/provision_propertyquarry_runtime_database.py",
                package.RUNTIME_DATABASE_HELPER_SHA256,
                package.RUNTIME_DATABASE_HELPER_BYTES,
            ),
            (
                "scripts/propertyquarry_runtime_isolation_v2.py",
                package.RUNTIME_ISOLATION_HELPER_SHA256,
                package.RUNTIME_ISOLATION_HELPER_BYTES,
            ),
            (
                "scripts/propertyquarry_runtime_deploy_v2.py",
                package.RUNTIME_DEPLOY_HELPER_SHA256,
                package.RUNTIME_DEPLOY_HELPER_BYTES,
            ),
        )
        for relative, digest, size in expected:
            with self.subTest(relative=relative):
                path = REPOSITORY_ROOT / relative
                metadata = path.lstat()
                raw = path.read_bytes()
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                self.assertFalse(stat.S_ISLNK(metadata.st_mode))
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual(len(raw), size)
                self.assertEqual(package.sha256(raw), digest)

    def test_runtime_database_helper_constants_match_tracked_git_blob(self) -> None:
        relative = "scripts/provision_propertyquarry_runtime_database.py"
        tree = subprocess.run(
            ["git", "ls-tree", FIXTURE_HELPER_COMMIT, "--", relative],
            cwd=REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        ).stdout.decode("ascii")
        self.assertEqual(
            tree,
            (
                "100755 blob "
                f"{RUNTIME_DATABASE_HELPER_GIT_BLOB}\t{relative}\n"
            ),
        )
        raw = subprocess.run(
            ["git", "cat-file", "blob", RUNTIME_DATABASE_HELPER_GIT_BLOB],
            cwd=REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        ).stdout
        self.assertEqual(len(raw), package.RUNTIME_DATABASE_HELPER_BYTES)
        self.assertEqual(
            package.sha256(raw),
            package.RUNTIME_DATABASE_HELPER_SHA256,
        )

    def _fixture_files(self) -> dict[str, Path]:
        package_public_pem = self._public_pem(self.package_private)
        receipt_public_pem = self._public_pem(self.receipt_private)
        _, _, package_key_id = package.load_public_key(
            package_public_pem, "fixture-package-public"
        )
        _, _, receipt_key_id = package.load_public_key(
            receipt_public_pem, "fixture-receipt-public"
        )
        identity = {
            "github_identity_env_digest": "sha256:" + "9" * 64,
            "github_identity_env_gid": 1000,
            "github_identity_env_mode": "0600",
            "github_identity_env_path": package.IDENTITY_ENV_PATH,
            "github_identity_env_uid": 1000,
        }
        registration_email = {
            "registration_email_env_digest": "sha256:" + "4" * 64,
            "registration_email_env_gid": 1000,
            "registration_email_env_mode": "0600",
            "registration_email_env_path": package.REGISTRATION_EMAIL_ENV_PATH,
            "registration_email_env_uid": 1000,
        }
        scene_video = {
            "scene_video_env_digest": "sha256:" + "3" * 64,
            "scene_video_env_gid": 1000,
            "scene_video_env_mode": 384,
            "scene_video_env_path": package.SCENE_VIDEO_ENV_PATH,
            "scene_video_env_uid": 1000,
        }
        cloudflared_image = "cloudflare/cloudflared@sha256:" + "6" * 64
        pre_purge_root_env_digest = "sha256:" + "5" * 64
        post_purge_root_env_digest = "sha256:" + "a" * 64
        deployment_id = "d" * 64
        backup_executable = package.PREDEPLOY_BACKUP_HELPER_PATH
        backup_helper = self._sealed_helper(
            "scripts/propertyquarry_predeploy_backup_v2.py"
        )
        database_executable = package.DATABASE_CONTROL_HELPER_PATH
        database_helper = self._sealed_helper(
            "scripts/propertyquarry_database_control_v2.py"
        )
        runtime_database_helper = self._sealed_helper(
            "scripts/provision_propertyquarry_runtime_database.py"
        )
        runtime_isolation_helper = self._sealed_helper(
            "scripts/propertyquarry_runtime_isolation_v2.py"
        )
        runtime_deploy_helper = self._sealed_helper(
            "scripts/propertyquarry_runtime_deploy_v2.py"
        )

        def runtime_input(path: str, digest: str, size: int) -> dict[str, object]:
            return {
                "gid": 1000,
                "mode": 384,
                "path": path,
                "sha256": digest,
                "size": size,
                "uid": 1000,
            }

        pre_purge_runtime_inputs = [
            runtime_input(package.BASE_ENV_PATH, pre_purge_root_env_digest, 2048),
            runtime_input(
                package.SCENE_VIDEO_ENV_PATH,
                scene_video["scene_video_env_digest"],
                1024,
            ),
            runtime_input(
                package.DATABASE_ROLES_ENV_PATH, "sha256:" + "b" * 64, 768
            ),
            runtime_input(
                package.ADMISSION_ENV_PATH, "sha256:" + "c" * 64, 512
            ),
            runtime_input(
                package.IDENTITY_ENV_PATH,
                identity["github_identity_env_digest"],
                384,
            ),
            runtime_input(
                package.REGISTRATION_EMAIL_ENV_PATH,
                registration_email["registration_email_env_digest"],
                256,
            ),
        ]
        runtime_inputs = [dict(item) for item in pre_purge_runtime_inputs]
        runtime_inputs[0]["sha256"] = post_purge_root_env_digest
        runtime_inputs[0]["size"] = 1024
        runtime_sha = "f25529e9927bdc3c49e111558a274b2aef2f797b"
        workflow_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        envelope_sha = "45e035d2cf3f67cd5a3d6aaa2da56e7765e5ad65" + "f" * 24
        web_image = (
            "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:"
            + "1" * 64
        )
        render_image = (
            "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:"
            + "2" * 64
        )
        runtime_retirement = {
            "containers": [],
            "deployment_id": deployment_id,
            "desired_live_allowlist": list(
                package.DESIRED_RUNTIME_CONTAINER_ALLOWLIST
            ),
            "operation": "retire-stale-propertyquarry-runtime",
            "preserve_volumes": True,
            "receipt_path": (
                f"{package.RUNTIME_ISOLATION_RECEIPT_ROOT}/{runtime_sha}/"
                f"{deployment_id}/retire-stale-propertyquarry-runtime.json"
            ),
        }

        def observed_file(
            path: str,
            digest_character: str,
            mode: str,
            uid: int,
            gid: int,
            size: int,
        ) -> dict[str, object]:
            return {
                "gid": gid,
                "mode": mode,
                "path": path,
                "sha256": "sha256:" + digest_character * 64,
                "size": size,
                "uid": uid,
            }

        runtime_deploy = {
            "compose_argv": package.expected_compose_argv(),
            "compose_files": [
                observed_file(
                    package.PROPERTY_COMPOSE_PATH, "0", "0644", 1000, 1000, 4096
                ),
                observed_file(
                    package.CLOUDFLARED_COMPOSE_PATH,
                    "1",
                    "0644",
                    1000,
                    1000,
                    2048,
                ),
            ],
            "compose_plugin": observed_file(
                package.DOCKER_COMPOSE_PLUGIN_PATH, "f", "0755", 0, 0, 8192
            ),
            "deployment_id": deployment_id,
            "docker_executable": observed_file(
                package.DOCKER_EXECUTABLE_PATH, "e", "0755", 0, 0, 16384
            ),
            "env_files": list(package.RUNTIME_INPUT_PATHS),
            "operation": "deploy-runtime",
            "receipt_path": (
                f"{package.RUNTIME_DEPLOY_RECEIPT_ROOT}/{runtime_sha}/"
                f"{deployment_id}/deploy-runtime.json"
            ),
        }
        database_substrate = {
            "container_id": "a" * 64,
            "container_name": package.DATABASE_CONTAINER_NAME,
            "database": package.DATABASE_NAME,
            "database_oid": 16384,
            "image": package.DATABASE_IMAGE,
            "image_id": "sha256:" + "d" * 64,
            "pgdata_volume": {
                "created_at": "2026-07-22T12:00:00Z",
                "driver": "local",
                "labels": {
                    "com.docker.compose.project": "property",
                    "com.docker.compose.volume": "propertyquarry_pgdata",
                },
                "mountpoint": package.DATABASE_PGDATA_VOLUME_MOUNTPOINT,
                "name": package.DATABASE_PGDATA_VOLUME_NAME,
                "options": {},
                "scope": "local",
            },
            "repo_digest": "postgres@sha256:"
            + package.DATABASE_IMAGE.rsplit("@sha256:", 1)[-1],
        }
        started = int(time.time()) - 1
        reservation_nonce = "0" * 64
        runner_label = "pqrelease-" + hashlib.sha256(
            package.RUNNER_LABEL_DERIVATION_DOMAIN
            + bytes.fromhex(reservation_nonce)
        ).hexdigest()[:32]
        source_checkout_identity = "sha256:" + "8" * 64
        source_tree_digest = "sha256:" + "9" * 64
        reservation_payload = {
            "authority_profile": package.PROFILE,
            "created_at_epoch": started - 60,
            "environment": package.ENVIRONMENT,
            "expires_at_epoch": started - 60 + package.RUNNER_RESERVATION_TTL_SECONDS,
            "receipt_authority_key_id": receipt_key_id,
            "release_job": package.RELEASE_JOB,
            "repository": package.REPOSITORY,
            "repository_id": package.REPOSITORY_ID,
            "repository_owner_id": package.REPOSITORY_OWNER_ID,
            "reservation_nonce": reservation_nonce,
            "runner_label": runner_label,
            "runner_label_nonce": runner_label.removeprefix("pqrelease-"),
            "schema": package.RUNNER_RESERVATION_SCHEMA,
            "source_checkout_identity_sha256": source_checkout_identity,
            "source_checkout_path": f"{package.RUNNER_RELEASE_CHECKOUT_ROOT}/{workflow_sha}",
            "source_tree_sha256": source_tree_digest,
            "version": 2,
            "workflow_path": ".github/workflows/smoke-runtime.yml",
            "workflow_ref": package.WORKFLOW_REF,
            "workflow_sha": workflow_sha,
        }
        reservation_canonical = package.canonical_json(reservation_payload)
        reservation_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_RESERVATION_SIGNATURE_DOMAIN,
                reservation_canonical,
            )
        )
        reservation_raw = package.canonical_json(
            {
                "payload": reservation_payload,
                "signature": base64.urlsafe_b64encode(reservation_signature)
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        prerequisite_job_id = "789"
        prerequisite_job_name = package._runner_prerequisite_job_name(
            runner_label=runner_label,
            reservation_sha256=package.sha256(reservation_raw),
        )
        prerequisite_intent_payload = {
            "authority_profile": package.PROFILE,
            "comment": "PropertyQuarry governed prerequisite approval "
            + package.sha256(reservation_raw),
            "discovered_at_epoch": started - 30,
            "environment_id": "42",
            "environment_name": package.ENVIRONMENT,
            "initial_jobs_sha256": "sha256:" + "1" * 64,
            "initial_pending_deployments_sha256": "sha256:" + "2" * 64,
            "initial_runs_index_sha256": "sha256:" + "3" * 64,
            "prerequisite_job_id": prerequisite_job_id,
            "prerequisite_job_key": package.RUNNER_PREREQUISITE_JOB_KEY,
            "prerequisite_job_name": prerequisite_job_name,
            "receipt_authority_key_id": receipt_key_id,
            "release_job": package.RELEASE_JOB,
            "repository": package.REPOSITORY,
            "repository_id": package.REPOSITORY_ID,
            "repository_owner_id": package.REPOSITORY_OWNER_ID,
            "reservation_expires_at_epoch": reservation_payload["expires_at_epoch"],
            "reservation_sha256": package.sha256(reservation_raw),
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": runner_label,
            "schema": package.RUNNER_PREREQUISITE_INTENT_SCHEMA_V3,
            "version": 3,
            "workflow_path": ".github/workflows/smoke-runtime.yml",
            "workflow_ref": package.WORKFLOW_REF,
            "workflow_sha": workflow_sha,
        }
        prerequisite_intent_canonical = package.canonical_json(
            prerequisite_intent_payload
        )
        prerequisite_intent_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_PREREQUISITE_INTENT_SIGNATURE_DOMAIN_V3,
                prerequisite_intent_canonical,
            )
        )
        prerequisite_intent_raw = package.canonical_json(
            {
                "payload": prerequisite_intent_payload,
                "signature": base64.urlsafe_b64encode(
                    prerequisite_intent_signature
                )
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        prerequisite_request_raw = package.canonical_json(
            {
                "comment": prerequisite_intent_payload["comment"],
                "environment_ids": [
                    int(prerequisite_intent_payload["environment_id"])
                ],
                "state": "approved",
            }
        )
        prerequisite_post_attempt_payload = {
            "attempted_at_epoch": started - 20,
            "authority_profile": package.PROFILE,
            "comment": prerequisite_intent_payload["comment"],
            "environment_id": prerequisite_intent_payload["environment_id"],
            "environment_name": package.ENVIRONMENT,
            "github_api_path": (
                f"/repos/{package.REPOSITORY}/actions/runs/"
                f"{prerequisite_intent_payload['run_id']}/pending_deployments"
            ),
            "http_method": "POST",
            "intent_sha256": package.sha256(prerequisite_intent_raw),
            "pre_post_jobs_sha256": "sha256:" + "8" * 64,
            "pre_post_pending_deployments_count": 1,
            "pre_post_pending_deployments_sha256": "sha256:" + "9" * 64,
            "pre_post_release_job_present": False,
            "pre_post_review_history_sha256": "sha256:" + "a" * 64,
            "pre_post_review_match_count": 0,
            "pre_post_review_scope": "any-approved-target-environment",
            "pre_post_run_sha256": "sha256:" + "b" * 64,
            "prerequisite_job_id": prerequisite_job_id,
            "prerequisite_job_key": package.RUNNER_PREREQUISITE_JOB_KEY,
            "prerequisite_job_name": prerequisite_job_name,
            "receipt_authority_key_id": receipt_key_id,
            "repository": package.REPOSITORY,
            "repository_id": package.REPOSITORY_ID,
            "repository_owner_id": package.REPOSITORY_OWNER_ID,
            "request_sha256": package.sha256(prerequisite_request_raw),
            "reservation_expires_at_epoch": reservation_payload[
                "expires_at_epoch"
            ],
            "reservation_sha256": package.sha256(reservation_raw),
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": runner_label,
            "schema": package.RUNNER_PREREQUISITE_POST_ATTEMPT_SCHEMA_V3,
            "version": 3,
            "workflow_path": ".github/workflows/smoke-runtime.yml",
            "workflow_ref": package.WORKFLOW_REF,
            "workflow_sha": workflow_sha,
        }
        prerequisite_post_attempt_canonical = package.canonical_json(
            prerequisite_post_attempt_payload
        )
        prerequisite_post_attempt_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_PREREQUISITE_POST_ATTEMPT_SIGNATURE_DOMAIN_V3,
                prerequisite_post_attempt_canonical,
            )
        )
        prerequisite_post_attempt_raw = package.canonical_json(
            {
                "payload": prerequisite_post_attempt_payload,
                "signature": base64.urlsafe_b64encode(
                    prerequisite_post_attempt_signature
                )
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        prerequisite_approval_payload = {
            "approval_api_disposition": "approved",
            "approval_response_sha256": "sha256:" + "4" * 64,
            "approved_at_epoch": started - 20,
            "completed_jobs_sha256": "sha256:" + "5" * 64,
            "environment_id": prerequisite_intent_payload["environment_id"],
            "environment_name": package.ENVIRONMENT,
            "intent_sha256": package.sha256(prerequisite_intent_raw),
            "post_pending_deployments_sha256": "sha256:" + "6" * 64,
            "prerequisite_conclusion": "success",
            "prerequisite_job_id": prerequisite_job_id,
            "prerequisite_job_key": package.RUNNER_PREREQUISITE_JOB_KEY,
            "prerequisite_job_name": prerequisite_job_name,
            "receipt_authority_key_id": receipt_key_id,
            "release_job": package.RELEASE_JOB,
            "repository": package.REPOSITORY,
            "repository_id": package.REPOSITORY_ID,
            "repository_owner_id": package.REPOSITORY_OWNER_ID,
            "reservation_expires_at_epoch": reservation_payload["expires_at_epoch"],
            "reservation_sha256": package.sha256(reservation_raw),
            "review_history_sha256": "sha256:" + "7" * 64,
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": runner_label,
            "schema": package.RUNNER_PREREQUISITE_APPROVAL_SCHEMA_V3,
            "version": 3,
            "workflow_path": ".github/workflows/smoke-runtime.yml",
            "workflow_ref": package.WORKFLOW_REF,
            "workflow_sha": workflow_sha,
        }
        prerequisite_approval_canonical = package.canonical_json(
            prerequisite_approval_payload
        )
        prerequisite_approval_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN_V3,
                prerequisite_approval_canonical,
            )
        )
        prerequisite_approval_raw = package.canonical_json(
            {
                "payload": prerequisite_approval_payload,
                "signature": base64.urlsafe_b64encode(
                    prerequisite_approval_signature
                )
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        plan = {
            "api_container_port": package.API_CONTAINER_PORT,
            "api_host_ip": package.API_HOST_IP,
            "api_host_port": package.API_HOST_PORT,
            "authority_profile": package.PROFILE,
            "backup_max_age_seconds": package.BACKUP_MAX_AGE_SECONDS,
            "cloudflared_image": cloudflared_image,
            "database_image": package.DATABASE_IMAGE,
            "database_substrate": database_substrate,
            "database_substrate_digest": package._canonical_digest(
                database_substrate
            ),
            "deployment_id": deployment_id,
            "envelope_sha": envelope_sha,
            "executables": {
                backup_executable: package.sha256(backup_helper),
                database_executable: package.sha256(database_helper),
                package.RUNTIME_DEPLOY_HELPER_PATH: package.sha256(
                    runtime_deploy_helper
                ),
                package.RUNTIME_ISOLATION_HELPER_PATH: package.sha256(
                    runtime_isolation_helper
                ),
            },
            **identity,
            "host_machine_id_digest": "sha256:" + "7" * 64,
            "post_purge_root_env_digest": post_purge_root_env_digest,
            "predecessor_runtime_sha": "genesis",
            "pre_purge_root_env_digest": pre_purge_root_env_digest,
            "pre_purge_runtime_inputs": pre_purge_runtime_inputs,
            "preflight_steps": [],
            "project_name": package.PROJECT_NAME,
            "public_origin": package.PUBLIC_ORIGIN,
            **registration_email,
            **scene_video,
            "release_generation": 1,
            "release_steps": [],
            "render_image": render_image,
            "repository": package.REPOSITORY,
            "runner_job_id": "456",
            "runner_label": runner_label,
            "runner_prerequisite_approval_payload_sha256": package.sha256(
                prerequisite_approval_canonical
            ),
            "runner_prerequisite_approval_sha256": package.sha256(
                prerequisite_approval_raw
            ),
            "runner_prerequisite_intent_sha256": package.sha256(
                prerequisite_intent_raw
            ),
            "runner_prerequisite_job_id": prerequisite_job_id,
            "runner_reservation_sha256": package.sha256(reservation_raw),
            "runner_run_attempt": 1,
            "runner_run_id": "123",
            "rollback_steps": [],
            "runtime_deploy": runtime_deploy,
            "runtime_deploy_digest": package._canonical_digest(runtime_deploy),
            "runtime_inputs": runtime_inputs,
            "runtime_retirement": runtime_retirement,
            "runtime_retirement_digest": package._canonical_digest(
                runtime_retirement
            ),
            "runtime_sha": runtime_sha,
            "workflow_sha": workflow_sha,
            "schema": package.PLAN_SCHEMA,
            "transaction_started_at_epoch": started,
            "verify_steps": [],
            "version": 2,
            "web_image": web_image,
        }

        def step(
            identifier: str,
            effect: str,
            argv: list[str],
            timeout_seconds: int,
        ) -> dict[str, object]:
            return {
                "argv": argv,
                "effect": effect,
                "expected_exit_code": 0,
                "id": identifier,
                "idempotent": True,
                "timeout_seconds": timeout_seconds,
            }

        plan["preflight_steps"] = [
            step(
                package.VERIFY_ISOLATION_INPUTS_STEP_ID,
                "read-only",
                package._expected_isolation_argv(
                    plan,
                    "verify-isolation-inputs",
                    receipt=False,
                    pre_purge=True,
                ),
                600,
            )
        ]
        backup_argv = [
            backup_executable,
            "create",
            "--runtime-sha",
            plan["runtime_sha"],
            "--deployment-id",
            deployment_id,
            "--envelope-sha",
            plan["envelope_sha"],
            "--web-image",
            plan["web_image"],
            "--render-image",
            plan["render_image"],
            "--database-image",
            plan["database_image"],
            "--receipt",
            f"{package.BACKUP_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/create.json",
            "--encryption-key",
            package.BACKUP_ENCRYPTION_KEY_PATH,
        ]
        release_steps = [
            step("predeploy-encrypted-backup", "mutation", backup_argv, 9600),
            step(
                "purge-propertyquarry-legacy-runtime-exposure",
                "mutation",
                package._expected_isolation_argv(
                    plan,
                    "purge-legacy-runtime-exposure",
                    receipt=True,
                    pre_purge=True,
                ),
                600,
            ),
            step(
                "retire-stale-propertyquarry-runtime",
                "mutation",
                package._expected_isolation_argv(
                    plan,
                    "retire-stale-propertyquarry-runtime",
                    receipt=True,
                    pre_purge=False,
                ),
                600,
            ),
        ]
        database_contracts = (
            (
                "provision-propertyquarry-database-roles",
                "provision-roles",
                900,
                "provision-roles.json",
            ),
            (
                "migrate-propertyquarry-schema",
                "migrate-schema",
                1500,
                "migrate-schema.json",
            ),
            (
                "harden-propertyquarry-runtime-acl",
                "harden-runtime-acl",
                900,
                "harden-runtime-acl.json",
            ),
            (
                "verify-propertyquarry-schema-readiness",
                "verify-schema-readiness",
                600,
                "verify-schema-readiness.json",
            ),
        )
        for identifier, operation, timeout_seconds, receipt_name in database_contracts:
            release_steps.append(
                step(
                    identifier,
                    "mutation",
                    [
                        database_executable,
                        operation,
                        "--runtime-sha",
                        runtime_sha,
                        "--deployment-id",
                        deployment_id,
                        "--web-image",
                        web_image,
                        "--database-image",
                        package.DATABASE_IMAGE,
                        "--receipt",
                        f"{package.DATABASE_RECEIPT_ROOT}/{runtime_sha}/"
                        f"{deployment_id}/{receipt_name}",
                    ],
                    timeout_seconds,
                )
            )
        release_steps.append(
            step(
                "deploy-propertyquarry-runtime",
                "mutation",
                package._expected_runtime_deploy_argv(plan),
                1800,
            )
        )
        plan["release_steps"] = release_steps
        plan["verify_steps"] = [
            step(
                package.TERMINAL_ISOLATION_VERIFY_STEP_ID,
                "verification",
                package._expected_isolation_argv(
                    plan,
                    "verify-runtime-isolation",
                    receipt=True,
                    pre_purge=False,
                ),
                600,
            )
        ]
        plan["rollback_steps"] = [
            step(
                package.ROLLBACK_ISOLATION_STEP_ID,
                "rollback",
                package._expected_isolation_argv(
                    plan,
                    "restore-legacy-runtime-exposure",
                    receipt=True,
                    pre_purge=True,
                ),
                600,
            )
        ]
        plan_raw = package.canonical_json(plan)
        config = {
            "allowed_runner_gid": 1001,
            "allowed_runner_uid": 1001,
            "api_container_port": package.API_CONTAINER_PORT,
            "api_host_ip": package.API_HOST_IP,
            "api_host_port": package.API_HOST_PORT,
            "authority_profile": package.PROFILE,
            "backup_max_age_seconds": package.BACKUP_MAX_AGE_SECONDS,
            "cloudflared_image": cloudflared_image,
            "database_image": package.DATABASE_IMAGE,
            "database_substrate": database_substrate,
            "database_substrate_digest": plan["database_substrate_digest"],
            "deployment_id": deployment_id,
            "envelope_sha": plan["envelope_sha"],
            "environment": package.ENVIRONMENT,
            "ephemeral_runner_label_prefix": "pqrelease-",
            "github_api_credential_path": (
                "/run/credentials/propertyquarry-release-single-host-v2.service/"
                "github-api-token"
            ),
            **identity,
            "github_oidc_request_origin": (
                "https://vstoken.actions.githubusercontent.com"
            ),
            "host_machine_id_digest": plan["host_machine_id_digest"],
            "package_authority_key_id": package_key_id,
            "plan_digest": package.sha256(plan_raw),
            "post_purge_root_env_digest": post_purge_root_env_digest,
            "predecessor_runtime_sha": plan["predecessor_runtime_sha"],
            "pre_purge_root_env_digest": pre_purge_root_env_digest,
            "pre_purge_runtime_inputs": pre_purge_runtime_inputs,
            "preflight_ttl_seconds": 120,
            "project_name": package.PROJECT_NAME,
            "public_origin": package.PUBLIC_ORIGIN,
            "receipt_authority_key_id": receipt_key_id,
            **registration_email,
            **scene_video,
            "release_generation": 1,
            "release_job": package.RELEASE_JOB,
            "render_image": plan["render_image"],
            "repository": package.REPOSITORY,
            "repository_id": package.REPOSITORY_ID,
            "repository_owner_id": package.REPOSITORY_OWNER_ID,
            "runner_job_id": plan["runner_job_id"],
            "runner_label": plan["runner_label"],
            "runner_prerequisite_approval_payload_sha256": plan[
                "runner_prerequisite_approval_payload_sha256"
            ],
            "runner_prerequisite_approval_sha256": plan[
                "runner_prerequisite_approval_sha256"
            ],
            "runner_prerequisite_intent_sha256": plan[
                "runner_prerequisite_intent_sha256"
            ],
            "runner_prerequisite_job_id": plan[
                "runner_prerequisite_job_id"
            ],
            "runner_reservation_sha256": plan["runner_reservation_sha256"],
            "runner_run_attempt": plan["runner_run_attempt"],
            "runner_run_id": plan["runner_run_id"],
            "runtime_deploy": runtime_deploy,
            "runtime_deploy_digest": plan["runtime_deploy_digest"],
            "runtime_inputs": runtime_inputs,
            "runtime_retirement": runtime_retirement,
            "runtime_retirement_digest": plan["runtime_retirement_digest"],
            "runtime_sha": plan["runtime_sha"],
            "schema": package.CONFIG_SCHEMA,
            "transaction_started_at_epoch": plan["transaction_started_at_epoch"],
            "version": 2,
            "web_image": plan["web_image"],
            "workflow_ref": package.WORKFLOW_REF,
            "workflow_sha": plan["workflow_sha"],
        }
        self.config_value = config
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        ticket_payload = {
            "authority_profile": package.PROFILE,
            "bound_at_epoch": started,
            "config_digest": package.sha256(config_raw),
            "dispatch_ticket_sha256": package.sha256(reservation_raw),
            "docker_socket": {
                "device": 1,
                "gid": 112,
                "inode": 2,
                "mode": "0660",
                "nlink": 1,
                "path": "/var/run/docker.sock",
                "uid": 0,
            },
            "environment": package.ENVIRONMENT,
            "expires_at_epoch": started + package.RUNNER_TICKET_TTL_SECONDS,
            "job_id": config["runner_job_id"],
            "plan_digest": package.sha256(plan_raw),
            "receipt_authority_key_id": receipt_key_id,
            "release_job": package.RELEASE_JOB,
            "repository": package.REPOSITORY,
            "repository_id": package.REPOSITORY_ID,
            "repository_owner_id": package.REPOSITORY_OWNER_ID,
            "reservation_nonce": reservation_nonce,
            "run_attempt": config["runner_run_attempt"],
            "run_id": config["runner_run_id"],
            "runner_image": config["web_image"],
            "runner_label": runner_label,
            "runner_label_nonce": runner_label.removeprefix("pqrelease-"),
            "runner_prerequisite_approval_payload_sha256": config[
                "runner_prerequisite_approval_payload_sha256"
            ],
            "runner_prerequisite_approval_sha256": config[
                "runner_prerequisite_approval_sha256"
            ],
            "runner_prerequisite_intent_sha256": config[
                "runner_prerequisite_intent_sha256"
            ],
            "runner_prerequisite_job_id": config[
                "runner_prerequisite_job_id"
            ],
            "runtime_sha": runtime_sha,
            "schema": package.RUNNER_LAUNCH_TICKET_SCHEMA,
            "version": 2,
            "workflow_path": ".github/workflows/smoke-runtime.yml",
            "workflow_ref": package.WORKFLOW_REF,
            "workflow_sha": workflow_sha,
        }
        ticket_canonical = package.canonical_json(ticket_payload)
        ticket_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_LAUNCH_TICKET_SIGNATURE_DOMAIN,
                ticket_canonical,
            )
        )
        runner_launch_ticket_raw = package.canonical_json(
            {
                "payload": ticket_payload,
                "signature": base64.urlsafe_b64encode(ticket_signature)
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        materialization_receipt = {
            "authoritative": False,
            "config_sha256": package.sha256(config_raw),
            "deployment_id": deployment_id,
            "final_artifact_id": "8536380028",
            "final_artifact_sha256": "1" * 64,
            "image_publication_run_attempt": "1",
            "image_publication_run_completed_at_epoch": started - 60,
            "image_publication_run_id": "29935868033",
            "installed_state_absence_proven": False,
            "materialized_at_epoch": started,
            "observation_completed_at_epoch": started,
            "package_authority_key_id": package_key_id,
            "plan_sha256": package.sha256(plan_raw),
            "preflight_artifact_id": "8536115693",
            "preflight_artifact_sha256": "2" * 64,
            "production_ready": False,
            "receipt_authority_key_id": receipt_key_id,
            "release_generation": 1,
            "release_hygiene_sha256": "3" * 64,
            "render_attestation_id": "36595704",
            "root_helper_authorization_required": True,
            "runner_launch_ticket_sha256": package.sha256(
                runner_launch_ticket_raw
            ),
            "runner_prerequisite_approval_payload_sha256": config[
                "runner_prerequisite_approval_payload_sha256"
            ],
            "runner_prerequisite_approval_sha256": config[
                "runner_prerequisite_approval_sha256"
            ],
            "runner_prerequisite_intent_sha256": config[
                "runner_prerequisite_intent_sha256"
            ],
            "runner_prerequisite_job_id": config[
                "runner_prerequisite_job_id"
            ],
            "runner_source_checkout_identity_sha256": source_checkout_identity,
            "runner_source_checkout_path": reservation_payload[
                "source_checkout_path"
            ],
            "runner_source_tree_sha256": source_tree_digest,
            "runtime_sha": runtime_sha,
            "schema": package.MATERIALIZATION_RECEIPT_SCHEMA,
            "valid_until_epoch": started + package.BACKUP_MAX_AGE_SECONDS,
            "version": 2,
            "web_attestation_id": "36595051",
            "workflow_sha": workflow_sha,
        }
        materialization_receipt_raw = package.canonical_json(
            materialization_receipt
        )
        materialization_receipt_signature = self.package_private.sign(
            package.framed(
                package.MATERIALIZATION_SIGNATURE_DOMAIN,
                materialization_receipt_raw,
            )
        )
        binary = self._synthetic_static_elf()
        build_receipt = {
            "authoritative": False,
            "binary_mode": "0755",
            "binary_sha256": package.sha256(binary),
            "binary_size": len(binary),
            "build_flags": [
                "-mod=readonly",
                "-trimpath",
                "-buildvcs=false",
                "-buildmode=exe",
            ],
            "go_tests_passed_in_both_builds": True,
            "host_network_namespace_isolated": False,
            "independent_toolchain_extractions": True,
            "installer_binary_mode": "0555",
            "installer_binary_sha256": "sha256:" + "5" * 64,
            "installer_binary_size": 4096,
            "installer_package_authority_bound": True,
            "installer_package_authority_key_id": package_key_id,
            "module_network_resolution_disabled": True,
            "package_signature_verified": False,
            "performs_release_effects": False,
            "production_ready": False,
            "receipt_published_last": True,
            "reproducible_double_build": True,
            "root_install_performed": False,
            "schema": package.BUILD_RECEIPT_SCHEMA,
            "scratch_execution_contract": "linux-amd64-static-et-exec-v1",
            "source_manifest_digest": "sha256:" + "6" * 64,
            "static_elf_verified_in_both_builds": True,
            "toolchain": "go1.26.5 linux/amd64",
            "toolchain_archive_bytes": 66879095,
            "toolchain_archive_sha256": (
                "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
            ),
            "version": 2,
        }
        return {
            "binary": self._write("controller", binary, 0o755),
            "predeploy_backup_helper": self._write(
                "propertyquarry-predeploy-backup-v2", backup_helper, 0o755
            ),
            "database_control_helper": self._write(
                "propertyquarry-database-control-v2", database_helper, 0o755
            ),
            "runtime_database_helper": self._write(
                "provision_propertyquarry_runtime_database.py",
                runtime_database_helper,
                0o755,
            ),
            "runtime_isolation_helper": self._write(
                "propertyquarry-runtime-isolation-v2",
                runtime_isolation_helper,
                0o755,
            ),
            "runtime_deploy_helper": self._write(
                "propertyquarry-runtime-deploy-v2",
                runtime_deploy_helper,
                0o755,
            ),
            "build_receipt": self._write(
                "build-receipt.json",
                package.canonical_json(build_receipt) + b"\n",
                0o644,
            ),
            "config": self._write("config.json", config_raw),
            "config_signature": self._write("config.sig", config_signature),
            "plan": self._write("plan.json", plan_raw),
            "materialization_receipt": self._write(
                "materialization-receipt.json", materialization_receipt_raw
            ),
            "materialization_receipt_signature": self._write(
                "materialization-receipt.sig", materialization_receipt_signature
            ),
            "runner_reservation": self._write(
                "runner-reservation.v2.json", reservation_raw
            ),
            "runner_launch_ticket": self._write(
                "runner-launch-ticket.v2.json", runner_launch_ticket_raw
            ),
            "runner_prerequisite_intent": self._write(
                "runner-prerequisite-intent.v3.json",
                prerequisite_intent_raw,
            ),
            "runner_prerequisite_post_attempt": self._write(
                "runner-prerequisite-post-attempt.v3.json",
                prerequisite_post_attempt_raw,
            ),
            "runner_prerequisite_approval": self._write(
                "runner-prerequisite-approval.v3.json",
                prerequisite_approval_raw,
            ),
            "package_public": self._write(
                "package-public.pem", package_public_pem, 0o644
            ),
            "package_private": self._write(
                "package-private.pem", self._private_pem(self.package_private)
            ),
            "receipt_public": self._write(
                "receipt-public.pem", receipt_public_pem, 0o644
            ),
            "receipt_private": self._write(
                "receipt-private.pem", self._private_pem(self.receipt_private)
            ),
        }

    def _build(self, name: str = "package.tar") -> Path:
        output = self.root / name
        result = package.build_package(
            argparse.Namespace(
                binary=str(self.paths["binary"]),
                predeploy_backup_helper=str(
                    self.paths["predeploy_backup_helper"]
                ),
                database_control_helper=str(
                    self.paths["database_control_helper"]
                ),
                runtime_database_helper=str(
                    self.paths["runtime_database_helper"]
                ),
                runtime_isolation_helper=str(
                    self.paths["runtime_isolation_helper"]
                ),
                runtime_deploy_helper=str(self.paths["runtime_deploy_helper"]),
                build_receipt=str(self.paths["build_receipt"]),
                config=str(self.paths["config"]),
                config_signature=str(self.paths["config_signature"]),
                plan=str(self.paths["plan"]),
                materialization_receipt=str(
                    self.paths["materialization_receipt"]
                ),
                materialization_receipt_signature=str(
                    self.paths["materialization_receipt_signature"]
                ),
                runner_reservation=str(self.paths["runner_reservation"]),
                runner_launch_ticket=str(self.paths["runner_launch_ticket"]),
                runner_prerequisite_intent=str(
                    self.paths["runner_prerequisite_intent"]
                ),
                runner_prerequisite_post_attempt=str(
                    self.paths["runner_prerequisite_post_attempt"]
                ),
                runner_prerequisite_approval=str(
                    self.paths["runner_prerequisite_approval"]
                ),
                package_authority_public_key=str(self.paths["package_public"]),
                package_authority_private_key=str(self.paths["package_private"]),
                receipt_authority_public_key=str(self.paths["receipt_public"]),
                receipt_authority_private_key=str(self.paths["receipt_private"]),
                output=str(output),
            )
        )
        self.assertFalse(result["authoritative"])
        self.assertFalse(result["production_ready"])
        self.assertFalse(result["performs_release_effects"])
        self.assertFalse(result["root_install_performed"])
        return output

    def _assert_rejected(self, function, expected: str | None = None) -> None:
        with self.assertRaises(package.PackageFailure) as caught:
            function()
        if expected is not None:
            self.assertIn(expected, str(caught.exception))

    def _validate_signed_values(
        self, config: dict[str, object], plan: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, object], str]:
        plan_raw = package.canonical_json(plan)
        config["plan_digest"] = package.sha256(plan_raw)
        config_raw = package.canonical_json(config)
        signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        package_public, _, package_key_id = package.load_public_key(
            self.paths["package_public"].read_bytes(), "package-public"
        )
        receipt_public, _, _ = package.load_public_key(
            self.paths["receipt_public"].read_bytes(), "receipt-public"
        )
        return package.validate_config_and_plan(
            config_raw,
            signature,
            plan_raw,
            package_public,
            package_key_id,
            receipt_public,
        )

    def _rewrite_archive(self, source: Path, transform) -> Path:
        with tarfile.open(source, mode="r:") as archive:
            entries = []
            for info in archive.getmembers():
                extracted = archive.extractfile(info)
                data = b"" if extracted is None else extracted.read()
                entries.append((info, data))
        entries = transform(entries)
        self.mutation_index += 1
        destination = self.root / (
            source.stem + f"-mutated-{self.mutation_index}.tar"
        )
        with tarfile.open(destination, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for info, data in entries:
                archive.addfile(info, io.BytesIO(data) if info.isreg() else None)
        os.chmod(destination, 0o400)
        return destination

    def test_deterministic_signed_package_verifies_and_stages_non_authoritatively(self) -> None:
        first = self._build("first.tar")
        second = self._build("second.tar")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o400)
        verified = package.verify_package(str(first), str(self.paths["package_public"]))
        self.assertEqual(
            verified.manifest["non_authoritative_until"],
            package.NON_AUTHORITATIVE_UNTIL,
        )
        self.assertTrue(verified.manifest["root_helper_verification_required"])
        self.assertFalse(verified.manifest["package_signing_private_key_included"])
        for field in (
            "api_container_port",
            "api_host_ip",
            "api_host_port",
            "backup_max_age_seconds",
            "cloudflared_image",
            "database_substrate_digest",
            "database_image",
            "deployment_id",
            "post_purge_root_env_digest",
            "pre_purge_root_env_digest",
            "render_image",
            "runtime_deploy_digest",
            "runtime_retirement_digest",
            "scene_video_env_path",
            "scene_video_env_mode",
            "scene_video_env_uid",
            "scene_video_env_gid",
            "scene_video_env_digest",
            "transaction_started_at_epoch",
            "web_image",
            "workflow_sha",
        ):
            self.assertEqual(verified.manifest[field], self.config_value[field])
        self.assertEqual(
            verified.manifest["pre_purge_runtime_inputs_digest"],
            package._canonical_digest(
                self.config_value["pre_purge_runtime_inputs"]
            ),
        )
        self.assertEqual(
            verified.manifest["runtime_inputs_digest"],
            package._canonical_digest(self.config_value["runtime_inputs"]),
        )
        package_paths = [entry["package_path"] for entry in verified.manifest["files"]]
        self.assertEqual(package_paths, sorted(package_paths))
        self.assertEqual(
            set(package_paths),
            {"payload" + path for path in package.PAYLOAD_LAYOUT_V3},
        )
        entries = {
            entry["install_path"]: entry for entry in verified.manifest["files"]
        }
        lifecycle_entry = entries[package.RUNNER_LIFECYCLE_INSTALL_PATH]
        self.assertEqual(lifecycle_entry["mode"], "0555")
        self.assertEqual(
            lifecycle_entry["purpose"],
            "ephemeral-runner-root-lifecycle",
        )
        self.assertEqual(
            lifecycle_entry["sha256"],
            package.sha256(
                (
                    MODULE_ROOT
                    / "tools/run-ephemeral-runner-with-docker.sh"
                ).read_bytes()
            ),
        )
        for helper_path, helper_digest, helper_size in (
            (
                package.PREDEPLOY_BACKUP_HELPER_PATH,
                package.PREDEPLOY_BACKUP_HELPER_SHA256,
                package.PREDEPLOY_BACKUP_HELPER_BYTES,
            ),
            (
                package.DATABASE_CONTROL_HELPER_PATH,
                package.DATABASE_CONTROL_HELPER_SHA256,
                package.DATABASE_CONTROL_HELPER_BYTES,
            ),
            (
                package.RUNTIME_DATABASE_HELPER_PATH,
                package.RUNTIME_DATABASE_HELPER_SHA256,
                package.RUNTIME_DATABASE_HELPER_BYTES,
            ),
        ):
            self.assertEqual(entries[helper_path]["mode"], "0755")
            self.assertEqual(entries[helper_path]["sha256"], helper_digest)
            self.assertEqual(entries[helper_path]["size"], helper_size)
        self.assertFalse(any("package-private" in name for name in verified.members))
        self.assertNotIn(self.paths["package_private"].read_bytes(), first.read_bytes())
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_ROOT / "tools" / "package.py"),
                "verify",
                "--package",
                str(first),
                "--package-authority-public-key",
                str(self.paths["package_public"]),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        cli_result = package.parse_strict_json(
            completed.stdout, "cli-result", trailing_newline=True
        )
        self.assertFalse(cli_result["authoritative"])
        self.assertFalse(cli_result["production_ready"])
        self.assertFalse(cli_result["performs_release_effects"])
        self.assertFalse(cli_result["root_install_performed"])
        stage = self.root / "stage"
        package.stage_package(verified, str(stage))
        self.assertEqual(
            stat.S_IMODE(
                (stage / "payload/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key").stat().st_mode
            ),
            0o400,
        )
        self.assertEqual(
            stat.S_IMODE(
                (stage / "payload/usr/libexec/propertyquarry-release-control/run-propertyquarry-ephemeral-runner-v2").stat().st_mode
            ),
            0o555,
        )
        staged_lifecycle = stage / (
            "payload" + package.RUNNER_LIFECYCLE_INSTALL_PATH
        )
        self.assertEqual(stat.S_IMODE(staged_lifecycle.stat().st_mode), 0o555)
        self.assertEqual(
            staged_lifecycle.read_bytes(),
            RUNNER_LIFECYCLE.read_bytes(),
        )
        for helper_path in (
            package.DATABASE_CONTROL_HELPER_PATH,
            package.RUNTIME_DATABASE_HELPER_PATH,
        ):
            staged_helper = stage / ("payload" + helper_path)
            self.assertTrue(staged_helper.is_file())
            self.assertEqual(stat.S_IMODE(staged_helper.stat().st_mode), 0o755)
        staged_database_control = stage / (
            "payload" + package.DATABASE_CONTROL_HELPER_PATH
        )
        installed_layout = subprocess.run(
            [sys.executable, str(staged_database_control), "--help"],
            cwd=self.root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "PYTHONPATH": "",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TZ": "UTC",
            },
            timeout=30,
        )
        self.assertEqual(
            installed_layout.returncode,
            0,
            installed_layout.stderr.decode("utf-8", "replace"),
        )
        self.assertIn(b"provision-roles", installed_layout.stdout)
        self.assertIn(b"verify-schema-readiness", installed_layout.stdout)

    def test_duplicate_and_noncanonical_signed_config_are_rejected(self) -> None:
        canonical = self.paths["config"].read_bytes()
        variants = [
            b'{"version":2,' + canonical[1:],
            canonical.replace(b'"version":2', b'"version" : 2'),
        ]
        for index, raw in enumerate(variants):
            with self.subTest(index=index):
                config_path = self._write(f"bad-config-{index}.json", raw)
                signature_path = self._write(
                    f"bad-config-{index}.sig",
                    self.package_private.sign(
                        package.framed(package.CONFIG_SIGNATURE_DOMAIN, raw)
                    ),
                )
                original_config = self.paths["config"]
                original_signature = self.paths["config_signature"]
                self.paths["config"] = config_path
                self.paths["config_signature"] = signature_path
                try:
                    self._assert_rejected(lambda: self._build(f"bad-{index}.tar"))
                finally:
                    self.paths["config"] = original_config
                    self.paths["config_signature"] = original_signature

    def test_prerequisite_raw_wrappers_and_semantic_chain_are_exact(self) -> None:
        archive = self._build("prerequisite-proof.tar")
        verified = package.verify_package(
            str(archive), str(self.paths["package_public"])
        )
        intent_member = (
            "payload/var/lib/propertyquarry-release-single-host-v2/"
            "runner-prerequisite-intent.v3.json"
        )
        approval_member = (
            "payload/var/lib/propertyquarry-release-single-host-v2/"
            "runner-prerequisite-approval.v3.json"
        )
        post_attempt_member = (
            "payload/var/lib/propertyquarry-release-single-host-v2/"
            "runner-prerequisite-post-attempt.v3.json"
        )
        self.assertEqual(
            verified.members[intent_member],
            self.paths["runner_prerequisite_intent"].read_bytes(),
        )
        self.assertEqual(
            verified.members[approval_member],
            self.paths["runner_prerequisite_approval"].read_bytes(),
        )
        self.assertEqual(
            verified.members[post_attempt_member],
            self.paths["runner_prerequisite_post_attempt"].read_bytes(),
        )
        self.assertEqual(verified.modes[intent_member], 0o400)
        self.assertEqual(verified.modes[approval_member], 0o400)
        for field in (
            "runner_prerequisite_approval_payload_sha256",
            "runner_prerequisite_approval_sha256",
            "runner_prerequisite_intent_sha256",
            "runner_prerequisite_job_id",
        ):
            self.assertEqual(verified.manifest[field], self.config_value[field])

        receipt_public, _, receipt_key_id = package.load_public_key(
            self.paths["receipt_public"].read_bytes(), "test-receipt-public"
        )
        binding = package.validate_runner_prerequisite_material_v3(
            intent_raw=verified.members[intent_member],
            post_attempt_raw=verified.members[post_attempt_member],
            approval_raw=verified.members[approval_member],
            reservation_raw=self.paths["runner_reservation"].read_bytes(),
            config=self.config_value,
            receipt_public=receipt_public,
            receipt_key_id=receipt_key_id,
        )
        self.assertEqual(
            binding["runner_prerequisite_job_id"],
            self.config_value["runner_prerequisite_job_id"],
        )
        self._assert_rejected(
            lambda: package.validate_runner_prerequisite_material_v3(
                intent_raw=verified.members[intent_member],
                post_attempt_raw=b"",
                approval_raw=verified.members[approval_member],
                reservation_raw=self.paths["runner_reservation"].read_bytes(),
                config=self.config_value,
                receipt_public=receipt_public,
                receipt_key_id=receipt_key_id,
            ),
            "runner-prerequisite-post-attempt",
        )

        intent_wire = package.parse_strict_json(
            verified.members[intent_member], "test-prerequisite-intent"
        )
        intent_v2_payload = dict(intent_wire["payload"])
        intent_v2_payload.pop("prerequisite_job_key")
        intent_v2_payload["prerequisite_job_name"] = (
            package.RUNNER_PREREQUISITE_JOB
        )
        intent_v2_payload["schema"] = (
            package.RUNNER_PREREQUISITE_INTENT_SCHEMA
        )
        intent_v2_payload["version"] = 2
        intent_v2_canonical = package.canonical_json(intent_v2_payload)
        intent_v2_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_PREREQUISITE_INTENT_SIGNATURE_DOMAIN,
                intent_v2_canonical,
            )
        )
        intent_v2_raw = package.canonical_json(
            {
                "payload": intent_v2_payload,
                "signature": base64.urlsafe_b64encode(intent_v2_signature)
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        approval_wire = package.parse_strict_json(
            verified.members[approval_member], "test-prerequisite-approval"
        )
        approval_v2_payload = dict(approval_wire["payload"])
        approval_v2_payload.pop("prerequisite_job_key")
        approval_v2_payload["intent_sha256"] = package.sha256(intent_v2_raw)
        approval_v2_payload["prerequisite_job_name"] = (
            package.RUNNER_PREREQUISITE_JOB
        )
        approval_v2_payload["schema"] = (
            package.RUNNER_PREREQUISITE_APPROVAL_SCHEMA
        )
        approval_v2_payload["version"] = 2
        approval_v2_canonical = package.canonical_json(approval_v2_payload)
        approval_v2_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN,
                approval_v2_canonical,
            )
        )
        approval_v2_raw = package.canonical_json(
            {
                "payload": approval_v2_payload,
                "signature": base64.urlsafe_b64encode(approval_v2_signature)
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        config_v2 = dict(self.config_value)
        config_v2["runner_prerequisite_intent_sha256"] = package.sha256(
            intent_v2_raw
        )
        config_v2["runner_prerequisite_approval_sha256"] = package.sha256(
            approval_v2_raw
        )
        config_v2[
            "runner_prerequisite_approval_payload_sha256"
        ] = package.sha256(approval_v2_canonical)
        historical = package.validate_runner_prerequisite_material(
            intent_raw=intent_v2_raw,
            approval_raw=approval_v2_raw,
            reservation_raw=self.paths["runner_reservation"].read_bytes(),
            config=config_v2,
            receipt_public=receipt_public,
            receipt_key_id=receipt_key_id,
        )
        self.assertEqual(
            historical["runner_prerequisite_job_id"],
            self.config_value["runner_prerequisite_job_id"],
        )

        tampered = bytearray(verified.members[approval_member])
        tampered[-2] ^= 1
        self._assert_rejected(
            lambda: package.validate_runner_prerequisite_material_v3(
                intent_raw=verified.members[intent_member],
                post_attempt_raw=verified.members[post_attempt_member],
                approval_raw=bytes(tampered),
                reservation_raw=self.paths["runner_reservation"].read_bytes(),
                config=self.config_value,
                receipt_public=receipt_public,
                receipt_key_id=receipt_key_id,
            )
        )

        approval_wire = package.parse_strict_json(
            verified.members[approval_member], "test-prerequisite-approval"
        )
        rebound_payload = dict(approval_wire["payload"])
        rebound_payload["prerequisite_conclusion"] = "failure"
        rebound_canonical = package.canonical_json(rebound_payload)
        rebound_signature = self.receipt_private.sign(
            package.framed(
                package.RUNNER_PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN_V3,
                rebound_canonical,
            )
        )
        rebound_raw = package.canonical_json(
            {
                "payload": rebound_payload,
                "signature": base64.urlsafe_b64encode(rebound_signature)
                .rstrip(b"=")
                .decode("ascii"),
                "signature_key_id": receipt_key_id,
            }
        )
        rebound_config = dict(self.config_value)
        rebound_config["runner_prerequisite_approval_sha256"] = package.sha256(
            rebound_raw
        )
        rebound_config[
            "runner_prerequisite_approval_payload_sha256"
        ] = package.sha256(rebound_canonical)
        self._assert_rejected(
            lambda: package.validate_runner_prerequisite_material_v3(
                intent_raw=verified.members[intent_member],
                post_attempt_raw=verified.members[post_attempt_member],
                approval_raw=rebound_raw,
                reservation_raw=self.paths["runner_reservation"].read_bytes(),
                config=rebound_config,
                receipt_public=receipt_public,
                receipt_key_id=receipt_key_id,
            ),
            "runner-prerequisite-approval-binding-invalid",
        )

        equal_jobs_config = package.parse_strict_json(
            self.paths["config"].read_bytes(), "equal-jobs-config"
        )
        equal_jobs_plan = package.parse_strict_json(
            self.paths["plan"].read_bytes(), "equal-jobs-plan"
        )
        equal_jobs_config["runner_prerequisite_job_id"] = equal_jobs_config[
            "runner_job_id"
        ]
        equal_jobs_plan["runner_prerequisite_job_id"] = equal_jobs_plan[
            "runner_job_id"
        ]
        self._assert_rejected(
            lambda: self._validate_signed_values(
                equal_jobs_config, equal_jobs_plan
            ),
            "config-runner-binding-invalid",
        )

    def test_manifest_layout_accepts_exact_v2_or_v3_and_rejects_mixed(
        self,
    ) -> None:
        archive = self._build("layout-versions.tar")
        verified = package.verify_package(
            str(archive), str(self.paths["package_public"])
        )
        _public, _raw, package_key_id = package.load_public_key(
            self.paths["package_public"].read_bytes(), "test-package-public"
        )
        manifest_v2 = package.parse_strict_json(
            package.canonical_json(verified.manifest), "test-v2-manifest"
        )
        post_path = (
            "/var/lib/propertyquarry-release-single-host-v2/"
            "runner-prerequisite-post-attempt.v3.json"
        )
        manifest_v2["files"] = [
            entry for entry in manifest_v2["files"]
            if entry["install_path"] != post_path
        ]
        for entry in manifest_v2["files"]:
            if entry["install_path"].endswith(
                "runner-prerequisite-intent.v3.json"
            ):
                entry["install_path"] = entry["install_path"].replace(
                    "intent.v3.json", "intent.v2.json"
                )
                entry["package_path"] = "payload" + entry["install_path"]
            elif entry["install_path"].endswith(
                "runner-prerequisite-approval.v3.json"
            ):
                entry["install_path"] = entry["install_path"].replace(
                    "approval.v3.json", "approval.v2.json"
                )
                entry["package_path"] = "payload" + entry["install_path"]
        manifest_v2["files"].sort(key=lambda entry: entry["package_path"])
        package._validate_manifest(manifest_v2, package_key_id)

        mixed = package.parse_strict_json(
            package.canonical_json(verified.manifest), "test-mixed-manifest"
        )
        mixed["files"] = [
            entry for entry in mixed["files"]
            if entry["install_path"] != post_path
        ]
        self._assert_rejected(
            lambda: package._validate_manifest(mixed, package_key_id),
            "manifest-file-set-invalid",
        )

    def test_canonical_json_matches_controller_string_contract(self) -> None:
        raw = package.canonical_json({"value": "<&>\u2028é", "version": 2})
        self.assertEqual(
            raw,
            b'{"value":"<&>\\u2028\\u00e9","version":2}',
        )
        self.assertEqual(package.parse_strict_json(raw, "fixture")["version"], 2)
        self._assert_rejected(
            lambda: package.parse_strict_json(
                b'{"value":1,"value":1}', "fixture"
            ),
            "json-duplicate-key",
        )

    def test_installer_receipt_requires_bound_key_or_explicit_unbound_state(self) -> None:
        original = self.paths["build_receipt"]
        receipt = package.parse_strict_json(
            original.read_bytes(), "build-receipt", trailing_newline=True
        )
        receipt["installer_package_authority_bound"] = False
        receipt["installer_package_authority_key_id"] = "unbound"
        self.paths["build_receipt"] = self._write(
            "unbound-installer-receipt.json",
            package.canonical_json(receipt) + b"\n",
            0o644,
        )
        artifact = self._build("unbound-installer-package.tar")
        package.verify_package(str(artifact), str(self.paths["package_public"]))

        receipt["installer_package_authority_bound"] = True
        receipt["installer_package_authority_key_id"] = "sha256:" + "0" * 64
        self.paths["build_receipt"] = self._write(
            "mismatched-installer-receipt.json",
            package.canonical_json(receipt) + b"\n",
            0o644,
        )
        self._assert_rejected(
            lambda: self._build("mismatched-installer-package.tar"),
            "build-receipt-installer-binding-invalid",
        )
        self.paths["build_receipt"] = original

    def test_plan_requires_ordered_database_gates_and_terminal_isolation(self) -> None:
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        plan["release_steps"][1], plan["release_steps"][2] = (
            plan["release_steps"][2],
            plan["release_steps"][1],
        )
        self._assert_rejected(
            lambda: package.validate_plan_steps(plan),
            "plan-release-order-invalid",
        )
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        plan["verify_steps"][-1]["id"] = "generic-verification"
        self._assert_rejected(
            lambda: package.validate_plan_steps(plan),
            "plan-terminal-isolation-verification-invalid",
        )
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        plan["release_steps"][4]["timeout_seconds"] = 1499
        self._assert_rejected(
            lambda: package.validate_plan_steps(plan),
            "plan-database-control-contract-invalid",
        )
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        deploy_argv = plan["release_steps"][7]["argv"]
        deploy_argv[2], deploy_argv[4] = deploy_argv[4], deploy_argv[2]
        self._assert_rejected(
            lambda: package.validate_plan_steps(plan),
            "plan-runtime-deploy-contract-invalid",
        )

    def test_runtime_and_workflow_git_identities_are_distinct_and_exact(self) -> None:
        package_public, _, package_key_id = package.load_public_key(
            self.paths["package_public"].read_bytes(), "package-public"
        )
        receipt_public, _, _ = package.load_public_key(
            self.paths["receipt_public"].read_bytes(), "receipt-public"
        )
        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        self.assertNotEqual(config["runtime_sha"], config["workflow_sha"])
        self.assertEqual(plan["workflow_sha"], config["workflow_sha"])

        config["workflow_sha"] = config["runtime_sha"]
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                self.paths["plan"].read_bytes(),
                package_public,
                package_key_id,
                receipt_public,
            ),
            "config-workflow_sha-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan["workflow_sha"] = plan["runtime_sha"]
        plan_raw = package.canonical_json(plan)
        config["plan_digest"] = package.sha256(plan_raw)
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                plan_raw,
                package_public,
                package_key_id,
                receipt_public,
            ),
            "plan-workflow_sha-binding-invalid",
        )

    def test_frozen_deployment_sequence_and_proof_paths_are_exact(self) -> None:
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        runtime_sha = plan["runtime_sha"]
        deployment_id = plan["deployment_id"]
        self.assertRegex(deployment_id, r"^[0-9a-f]{64}$")
        self.assertEqual(
            tuple(step["id"] for step in plan["release_steps"]),
            package.REQUIRED_RELEASE_STEP_IDS,
        )
        self.assertEqual(len(plan["preflight_steps"]), 1)
        self.assertEqual(len(plan["release_steps"]), 8)
        self.assertEqual(len(plan["verify_steps"]), 1)
        self.assertEqual(len(plan["rollback_steps"]), 1)
        self.assertEqual(
            plan["preflight_steps"][0]["id"],
            package.VERIFY_ISOLATION_INPUTS_STEP_ID,
        )
        self.assertEqual(
            plan["verify_steps"][0]["id"],
            package.TERMINAL_ISOLATION_VERIFY_STEP_ID,
        )
        self.assertEqual(
            plan["rollback_steps"][0]["id"],
            package.ROLLBACK_ISOLATION_STEP_ID,
        )
        for step in [
            *plan["release_steps"],
            *plan["verify_steps"],
            *plan["rollback_steps"],
        ]:
            argv = step["argv"]
            if "--receipt" not in argv:
                continue
            receipt = argv[argv.index("--receipt") + 1]
            self.assertIn(f"/{runtime_sha}/{deployment_id}/", receipt)
        self.assertNotIn("--receipt", plan["preflight_steps"][0]["argv"])
        self.assertEqual(
            set(plan["executables"]),
            {
                package.PREDEPLOY_BACKUP_HELPER_PATH,
                package.RUNTIME_ISOLATION_HELPER_PATH,
                package.DATABASE_CONTROL_HELPER_PATH,
                package.RUNTIME_DEPLOY_HELPER_PATH,
            },
        )

        mutations = (
            (0, "plan-predeploy-backup-contract-invalid"),
            (1, "plan-runtime-purge-contract-invalid"),
            (2, "plan-runtime-retirement-contract-invalid"),
            (3, "plan-database-control-contract-invalid"),
            (7, "plan-runtime-deploy-contract-invalid"),
        )
        for release_index, expected in mutations:
            with self.subTest(release_index=release_index):
                mutated = package.parse_strict_json(
                    self.paths["plan"].read_bytes(), "plan"
                )
                argv = mutated["release_steps"][release_index]["argv"]
                receipt_index = argv.index("--receipt") + 1
                argv[receipt_index] = argv[receipt_index].replace(
                    f"/{deployment_id}/", "/"
                )
                self._assert_rejected(
                    lambda mutated=mutated: package.validate_plan_steps(mutated),
                    expected,
                )

    def test_signed_runtime_transition_and_observed_contracts_fail_closed(self) -> None:
        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        config["deployment_id"] = "D" * 64
        self._assert_rejected(
            lambda: self._validate_signed_values(config, plan),
            "config-deployment_id-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        config["runtime_inputs"][2]["size"] += 1
        self._assert_rejected(
            lambda: self._validate_signed_values(config, plan),
            "runtime-input-transition-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        config["runtime_retirement_digest"] = "sha256:" + "0" * 64
        self._assert_rejected(
            lambda: self._validate_signed_values(config, plan),
            "config-observed-contract-digest-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        config["database_substrate"]["pgdata_volume"]["name"] = "wrong-volume"
        self._assert_rejected(
            lambda: self._validate_signed_values(config, plan),
            "database-substrate-binding-invalid",
        )

    def test_resigned_manifest_cannot_rebind_deployment_window_or_inputs(self) -> None:
        source = self._build("deployment-binding-source.tar")

        def substitute(entries):
            manifest_raw = next(
                data for info, data in entries if info.name == "manifest.v2.json"
            )
            manifest = package.parse_strict_json(manifest_raw, "manifest")
            manifest["deployment_id"] = "e" * 64
            manifest["transaction_started_at_epoch"] += 1
            manifest["runtime_inputs_digest"] = "sha256:" + "f" * 64
            rebound_manifest = package.canonical_json(manifest)
            rebound_signature = self.package_private.sign(
                package.framed(
                    package.MANIFEST_SIGNATURE_DOMAIN,
                    rebound_manifest,
                )
            )
            result = []
            for info, data in entries:
                if info.name == "manifest.v2.json":
                    data = rebound_manifest
                elif info.name == "manifest.v2.sig":
                    data = rebound_signature
                info.size = len(data)
                result.append((info, data))
            return result

        rebound = self._rewrite_archive(source, substitute)
        self._assert_rejected(
            lambda: package.verify_package(
                str(rebound), str(self.paths["package_public"])
            ),
            "manifest-profile-binding-invalid",
        )

    def test_cloudflared_scene_video_and_manifest_bindings_are_exact(self) -> None:
        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        config["api_host_port"] = 8090
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        package_public, _, package_key_id = package.load_public_key(
            self.paths["package_public"].read_bytes(), "package-public"
        )
        receipt_public, _, _ = package.load_public_key(
            self.paths["receipt_public"].read_bytes(), "receipt-public"
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                self.paths["plan"].read_bytes(),
                package_public,
                package_key_id,
                receipt_public,
            ),
            "config-api_host_port-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        config["database_image"] = "postgres:16-alpine@sha256:" + "0" * 64
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                self.paths["plan"].read_bytes(),
                package_public,
                package_key_id,
                receipt_public,
            ),
            "config-database_image-binding-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        plan["database_image"] = "postgres:16-alpine@sha256:" + "0" * 64
        plan_raw = package.canonical_json(plan)
        config["plan_digest"] = package.sha256(plan_raw)
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                plan_raw,
                package_public,
                package_key_id,
                receipt_public,
            ),
            "plan-database_image-binding-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        plan["cloudflared_image"] = "cloudflare/cloudflared@sha256:" + "7" * 64
        plan_raw = package.canonical_json(plan)
        config["plan_digest"] = package.sha256(plan_raw)
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                plan_raw,
                package_public,
                package_key_id,
                receipt_public,
            ),
            "plan-cloudflared_image-binding-invalid",
        )

        config["cloudflared_image"] = "cloudflare/cloudflared:latest"
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                plan_raw,
                package_public,
                package_key_id,
                receipt_public,
            ),
            "config-image-binding-invalid",
        )

        config = package.parse_strict_json(self.paths["config"].read_bytes(), "config")
        config["scene_video_env_mode"] = 0o644
        config_raw = package.canonical_json(config)
        config_signature = self.package_private.sign(
            package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
        self._assert_rejected(
            lambda: package.validate_config_and_plan(
                config_raw,
                config_signature,
                self.paths["plan"].read_bytes(),
                package_public,
                package_key_id,
                receipt_public,
            ),
            "config-scene_video_env_mode-invalid",
        )
        plan = package.parse_strict_json(self.paths["plan"].read_bytes(), "plan")
        plan["release_steps"][6]["argv"][-1] = "wrong-receipt.json"
        self._assert_rejected(
            lambda: package.validate_plan_steps(plan),
            "plan-database-control-contract-invalid",
        )

    def test_resigned_manifest_cannot_rebind_database_image(self) -> None:
        source = self._build("database-image-source.tar")

        def substitute(entries):
            manifest_raw = next(
                data for info, data in entries if info.name == "manifest.v2.json"
            )
            manifest = package.parse_strict_json(manifest_raw, "manifest")
            manifest["database_image"] = "postgres:16-alpine@sha256:" + "0" * 64
            rebound_manifest = package.canonical_json(manifest)
            rebound_signature = self.package_private.sign(
                package.framed(
                    package.MANIFEST_SIGNATURE_DOMAIN,
                    rebound_manifest,
                )
            )
            result = []
            for info, data in entries:
                if info.name == "manifest.v2.json":
                    data = rebound_manifest
                elif info.name == "manifest.v2.sig":
                    data = rebound_signature
                info.size = len(data)
                result.append((info, data))
            return result

        rebound = self._rewrite_archive(source, substitute)
        self._assert_rejected(
            lambda: package.verify_package(
                str(rebound),
                str(self.paths["package_public"]),
            ),
            "manifest-binding-invalid",
        )

    def test_build_rejects_database_helper_byte_substitution(self) -> None:
        original = self.paths["database_control_helper"]
        raw = original.read_bytes()
        tampered = self._write(
            "propertyquarry-database-control-v2-tampered",
            raw[:-1] + bytes([raw[-1] ^ 1]),
            0o755,
        )
        self.paths["database_control_helper"] = tampered
        try:
            self._assert_rejected(
                lambda: self._build("tampered-database-helper.tar"),
                "database-control-helper-sealed-bytes-invalid",
            )
        finally:
            self.paths["database_control_helper"] = original

    def test_build_rejects_isolation_and_deploy_helper_byte_substitution(self) -> None:
        contracts = (
            ("runtime_isolation_helper", "runtime-isolation-helper-sealed-bytes-invalid"),
            ("runtime_deploy_helper", "runtime-deploy-helper-sealed-bytes-invalid"),
        )
        for path_key, expected in contracts:
            with self.subTest(path_key=path_key):
                original = self.paths[path_key]
                raw = original.read_bytes()
                tampered = self._write(
                    f"{path_key}-tampered-{self.mutation_index}",
                    raw[:-1] + bytes([raw[-1] ^ 1]),
                    0o755,
                )
                self.mutation_index += 1
                self.paths[path_key] = tampered
                try:
                    self._assert_rejected(
                        lambda: self._build(f"tampered-{path_key}.tar"),
                        expected,
                    )
                finally:
                    self.paths[path_key] = original

    def test_archive_rejects_extra_unsafe_duplicate_symlink_hash_and_mode(self) -> None:
        source = self._build()

        def extra(entries):
            info = tarfile.TarInfo("extra")
            info.size = 1
            info.mode = 0o444
            info.uid = info.gid = info.mtime = 0
            return entries + [(info, b"x")]

        def unsafe(entries):
            info = tarfile.TarInfo("../escape")
            info.size = 1
            info.mode = 0o444
            info.uid = info.gid = info.mtime = 0
            return entries + [(info, b"x")]

        def duplicate(entries):
            info, data = entries[-1]
            clone = tarfile.TarInfo(info.name)
            clone.size = len(data)
            clone.mode = info.mode
            clone.uid = clone.gid = clone.mtime = 0
            return entries + [(clone, data)]

        def symlink(entries):
            info, _data = entries[-1]
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            info.size = 0
            return entries

        def bad_hash(entries):
            info, data = entries[-1]
            return entries[:-1] + [(info, data[:-1] + bytes([data[-1] ^ 1]))]

        def bad_mode(entries):
            info, data = entries[-1]
            info.mode ^= 0o002
            return entries[:-1] + [(info, data)]

        for name, transform in (
            ("extra", extra),
            ("unsafe", unsafe),
            ("duplicate", duplicate),
            ("symlink", symlink),
            ("hash", bad_hash),
            ("mode", bad_mode),
        ):
            with self.subTest(name=name):
                mutated = self._rewrite_archive(source, transform)
                self._assert_rejected(
                    lambda: package.verify_package(
                        str(mutated), str(self.paths["package_public"])
                    )
                )

    def test_external_anchor_is_required_and_staging_never_overwrites(self) -> None:
        source = self._build()
        os.chmod(source, 0o444)
        self._assert_rejected(
            lambda: package.verify_package(
                str(source), str(self.paths["package_public"])
            ),
            "input-mode-invalid",
        )
        os.chmod(source, 0o400)
        other_key = Ed25519PrivateKey.generate()
        other_anchor = self._write(
            "other-anchor.pem", self._public_pem(other_key), 0o644
        )
        self._assert_rejected(
            lambda: package.verify_package(str(source), str(other_anchor)),
            "manifest-signature-invalid",
        )
        verified = package.verify_package(str(source), str(self.paths["package_public"]))
        stage = self.root / "occupied-stage"
        stage.mkdir(mode=0o700)
        sentinel = stage / "sentinel"
        sentinel.write_text("preserve", encoding="ascii")
        self._assert_rejected(
            lambda: package.stage_package(verified, str(stage)),
            "stage-already-exists",
        )
        self.assertEqual(sentinel.read_text(encoding="ascii"), "preserve")

    def test_systemd_units_have_valid_syntax_and_strict_boundaries(self) -> None:
        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze unavailable")
        source = MODULE_ROOT / "packaging" / "templates"
        units = self.root / "units"
        units.mkdir(mode=0o700)
        socket_raw = (
            source / "propertyquarry-release-single-host-v2.socket"
        ).read_bytes()
        service_raw = (
            source / "propertyquarry-release-single-host-v2@.service"
        ).read_bytes()
        service_raw = service_raw.replace(
            b"ExecStart=/usr/libexec/propertyquarry-release-control/"
            b"propertyquarry-release-single-host-v2 serve",
            b"ExecStart=/bin/true",
        )
        socket_path = units / "propertyquarry-release-single-host-v2.socket"
        service_path = units / "propertyquarry-release-single-host-v2@.service"
        socket_path.write_bytes(socket_raw)
        service_path.write_bytes(service_raw)
        completed = subprocess.run(
            [analyzer, "verify", str(socket_path), str(service_path)],
            cwd=units,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout.decode("utf-8"))
        self.assertIn(b"Accept=yes", socket_raw)
        self.assertIn(b"User=root", service_raw)
        self.assertIn(b"ProtectSystem=strict", service_raw)
        self.assertIn(b"SystemCallFilter=@system-service", service_raw)
        self.assertIn(
            b"LoadCredentialEncrypted=github-api-token:"
            b"/etc/propertyquarry-release-single-host-v2/github-api-token.cred",
            service_raw,
        )
        self.assertIn(
            b"ReadWritePaths=/var/lib/propertyquarry-release-single-host-v2",
            service_raw,
        )
        self.assertIn(
            b"ReadWritePaths=/docker/property/state/runtime",
            service_raw,
        )
        self.assertIn(
            b"ReadOnlyPaths=/docker/property/state/runtime/propertyquarry_admission.env",
            service_raw,
        )
        self.assertIn(
            b"ReadOnlyPaths=/docker/property/state/runtime/property_scene_video_shared.env",
            service_raw,
        )
        self.assertIn(
            b"ReadWritePaths=/docker/property/.env",
            service_raw,
        )
        self.assertIn(
            b"ReadWritePaths=/mnt/pcloud/propertyquarry/releases/backups/v2",
            service_raw,
        )
        self.assertIn(
            b"CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH",
            service_raw,
        )
        sysusers = shutil.which("systemd-sysusers")
        if sysusers is not None:
            rendered = package.render_templates(self.config_value)["sysusers"]
            rendered_path = self._write("rendered-sysusers.conf", rendered, 0o644)
            alternate_root = self.root / "sysusers-root"
            alternate_root.mkdir(mode=0o700)
            completed = subprocess.run(
                [
                    sysusers,
                    "--dry-run",
                    f"--root={alternate_root}",
                    str(rendered_path),
                ],
                cwd=self.root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                timeout=15,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stdout.decode("utf-8")
            )
        tmpfiles = shutil.which("systemd-tmpfiles")
        if tmpfiles is not None:
            tmpfiles_raw = (
                source / "propertyquarry-release-single-host-v2.tmpfiles.conf"
            ).read_bytes()
            self.assertIn(
                b"d /var/lib/propertyquarry-release-single-host-v2/journal "
                b"0700 root root - -\n",
                tmpfiles_raw,
            )
            self.assertIn(
                b"d /var/lib/propertyquarry-release-single-host-v2/database-receipts "
                b"0700 root root - -\n",
                tmpfiles_raw,
            )
            self.assertIn(
                b"d /var/lib/propertyquarry-release-single-host-v2/"
                b"ai-panorama-publication-locks 0700 root root - -\n",
                tmpfiles_raw,
            )
            neutral_lines = []
            for raw_line in tmpfiles_raw.decode("ascii").splitlines():
                fields = raw_line.split()
                if fields and not fields[0].startswith("#"):
                    fields[3] = "-"
                    fields[4] = "-"
                    raw_line = " ".join(fields)
                neutral_lines.append(raw_line)
            neutral_path = self._write(
                "neutral-tmpfiles.conf",
                ("\n".join(neutral_lines) + "\n").encode("ascii"),
                0o644,
            )
            alternate_root = self.root / "tmpfiles-root"
            alternate_root.mkdir(mode=0o700)
            completed = subprocess.run(
                [
                    tmpfiles,
                    "--create",
                    f"--root={alternate_root}",
                    str(neutral_path),
                ],
                cwd=self.root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                timeout=15,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stdout.decode("utf-8")
            )
            journal = (
                alternate_root
                / "var/lib/propertyquarry-release-single-host-v2/journal"
            )
            self.assertTrue(journal.is_dir())
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o700)

    @unittest.skipUnless(
        os.environ.get("PROPERTYQUARRY_REAL_BUILD_DIR"),
        "PROPERTYQUARRY_REAL_BUILD_DIR not provided",
    )
    def test_real_reproducible_build_is_accepted(self) -> None:
        build_directory = Path(os.environ["PROPERTYQUARRY_REAL_BUILD_DIR"])
        self.paths["binary"] = (
            build_directory / "propertyquarry-release-single-host-v2"
        )
        self.paths["build_receipt"] = build_directory / "build-receipt.v2.json"
        installer = (
            build_directory / "propertyquarry-release-single-host-installer-v2"
        )
        receipt = package.parse_strict_json(
            self.paths["build_receipt"].read_bytes(),
            "build-receipt",
            trailing_newline=True,
        )
        installer_raw = installer.read_bytes()
        self.assertEqual(stat.S_IMODE(installer.stat().st_mode), 0o555)
        self.assertEqual(receipt["installer_binary_size"], len(installer_raw))
        self.assertEqual(receipt["installer_binary_sha256"], package.sha256(installer_raw))
        artifact = self._build("real-build-package.tar")
        verified = package.verify_package(
            str(artifact), str(self.paths["package_public"])
        )
        binary_name = (
            "payload/usr/libexec/propertyquarry-release-control/"
            "propertyquarry-release-single-host-v2"
        )
        binary_entry = next(
            entry
            for entry in verified.manifest["files"]
            if entry["package_path"] == binary_name
        )
        self.assertEqual(
            binary_entry["sha256"], package.sha256(self.paths["binary"].read_bytes())
        )


if __name__ == "__main__":
    unittest.main()
