from __future__ import annotations

import concurrent.futures
import base64
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
import warnings
import zipfile

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_single_host_materialize_tests",
    MODULE_ROOT / "tools" / "materialize.py",
)
assert SPEC is not None and SPEC.loader is not None
materialize = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = materialize
SPEC.loader.exec_module(materialize)


class SimulatedBootstrapCrash(RuntimeError):
    pass


class MaterializeBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-materialize-test-"
        )
        self.parent = Path(self.temporary.name)
        os.chmod(self.parent, 0o700)
        self.target = self.parent / "single-host-v2-receipt-authority"
        self.package_private = Ed25519PrivateKey.generate()
        self.package_anchor = self.package_private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        _der, self.package_id = materialize._public_identity(
            self.package_private.public_key()
        )
        self.patches = [
            mock.patch.object(
                materialize, "PRODUCTION_RECEIPT_AUTHORITY_ROOT", self.target
            ),
            mock.patch.object(
                materialize,
                "_load_canonical_package_authority",
                side_effect=self._canonical_authority,
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def _canonical_authority(self):
        return (
            self.package_private,
            self.package_private.public_key(),
            self.package_anchor,
            self.package_id,
        )

    def _persisted_receipt_id(self) -> str | None:
        _target, stage, _lock = materialize._bootstrap_stage_paths()
        candidates = (
            self.target / "receipt-authority-v2.key",
            stage / "authority" / "receipt-authority-v2.key",
            stage / "authority" / ".receipt-authority-v2.key.pending",
        )
        for path in candidates:
            if not path.exists():
                continue
            raw = path.read_bytes()
            try:
                key = serialization.load_pem_private_key(raw, password=None)
            except (TypeError, ValueError):
                continue
            if isinstance(key, Ed25519PrivateKey):
                return materialize._public_identity(key.public_key())[1]
        return None

    def _bootstrap(self, now: int = 1_900_000_000):
        return materialize.bootstrap_authority(
            authority_root=os.fspath(self.target), now=now
        )

    def test_bootstrap_resumes_same_key_across_every_durable_boundary(self) -> None:
        points = (
            "after-stage-directory",
            "after-intent",
            "after-authority-stage-directory",
            "after-key-pending",
            "after-key-promote",
            "after-authority-file-authority-bootstrap.v2.json",
            "after-authority-file-authority-bootstrap.v2.sig",
            "after-authority-file-receipt-authority-v2.key",
            "after-authority-file-receipt-authority-v2.pem",
            "before-authority-promote",
            "after-authority-promote",
        )
        for point in points:
            with self.subTest(point=point):
                case = self.parent / point.replace("/", "-")
                case.mkdir(mode=0o700)
                prior_target = self.target
                self.target = case / "single-host-v2-receipt-authority"
                with mock.patch.object(
                    materialize,
                    "PRODUCTION_RECEIPT_AUTHORITY_ROOT",
                    self.target,
                ):
                    def checkpoint(candidate: str) -> None:
                        if candidate == point:
                            raise SimulatedBootstrapCrash(point)

                    with mock.patch.object(
                        materialize, "_bootstrap_checkpoint", checkpoint
                    ):
                        with self.assertRaises(SimulatedBootstrapCrash):
                            self._bootstrap()
                    persisted = self._persisted_receipt_id()
                    with mock.patch.object(
                        materialize,
                        "_bootstrap_checkpoint",
                        lambda _candidate: None,
                    ):
                        result = self._bootstrap(now=1_900_000_111)
                    self.assertEqual(
                        result["receipt_authority_key_id"],
                        materialize._load_authority(os.fspath(self.target))[6],
                    )
                    if persisted is not None:
                        self.assertEqual(result["receipt_authority_key_id"], persisted)
                    _target, stage, _lock = materialize._bootstrap_stage_paths()
                    self.assertFalse(stage.exists())
                    self.assertEqual(
                        {item.name for item in self.target.iterdir()},
                        set(materialize.AUTHORITY_FILES),
                    )
                    self.assertFalse(
                        any(".materializing." in item.name for item in case.iterdir())
                    )
                self.target = prior_target

    def test_torn_pending_key_is_replaced_but_promoted_key_is_never_rotated(self) -> None:
        def checkpoint(candidate: str) -> None:
            if candidate == "after-key-pending":
                raise SimulatedBootstrapCrash(candidate)

        with mock.patch.object(materialize, "_bootstrap_checkpoint", checkpoint):
            with self.assertRaises(SimulatedBootstrapCrash):
                self._bootstrap()
        _target, stage, _lock = materialize._bootstrap_stage_paths()
        pending = stage / "authority" / ".receipt-authority-v2.key.pending"
        os.chmod(pending, 0o600)
        with pending.open("r+b") as stream:
            stream.truncate(9)
        os.chmod(pending, 0o400)
        result = self._bootstrap(now=1_900_000_123)
        self.assertEqual(
            result["receipt_authority_key_id"],
            materialize._load_authority(os.fspath(self.target))[6],
        )

    def test_deterministic_auxiliary_repair_preserves_promoted_staged_key(self) -> None:
        def checkpoint(candidate: str) -> None:
            if candidate == "after-authority-file-authority-bootstrap.v2.json":
                raise SimulatedBootstrapCrash(candidate)

        with mock.patch.object(materialize, "_bootstrap_checkpoint", checkpoint):
            with self.assertRaises(SimulatedBootstrapCrash):
                self._bootstrap()
        persisted = self._persisted_receipt_id()
        self.assertIsNotNone(persisted)
        _target, stage, _lock = materialize._bootstrap_stage_paths()
        receipt = stage / "authority" / "authority-bootstrap.v2.json"
        os.chmod(receipt, 0o600)
        with receipt.open("r+b") as stream:
            stream.truncate(11)
        os.chmod(receipt, 0o400)
        result = self._bootstrap(now=1_900_000_456)
        self.assertEqual(result["receipt_authority_key_id"], persisted)

    def test_concurrent_bootstraps_serialize_to_one_stable_authority(self) -> None:
        def invoke(index: int):
            return self._bootstrap(now=1_900_000_000 + index)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(invoke, range(8)))
        self.assertEqual(
            len({result["receipt_authority_key_id"] for result in results}), 1
        )
        self.assertEqual(sum(result["authority_created"] for result in results), 1)
        self.assertEqual(
            results[0]["receipt_authority_key_id"],
            materialize._load_authority(os.fspath(self.target))[6],
        )

    def test_alternate_authority_root_is_rejected_before_creation(self) -> None:
        alternate = self.parent / "alternate"
        with self.assertRaisesRegex(
            materialize.MaterializeFailure,
            "production-receipt-authority-root-invalid",
        ):
            materialize.bootstrap_authority(
                authority_root=os.fspath(alternate), now=1_900_000_000
            )
        self.assertFalse(alternate.exists())

    def test_real_sigkill_inner_boundaries_converge_without_private_remnants(self) -> None:
        helper = textwrap.dedent(
            """
            import importlib.util,json,os,pathlib,signal,sys
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            module_root=pathlib.Path(sys.argv[1])
            target=pathlib.Path(sys.argv[2])
            key_path=pathlib.Path(sys.argv[3])
            point=sys.argv[4]
            partial=sys.argv[5] == "partial"
            spec=importlib.util.spec_from_file_location("pq_materialize_kill_helper",module_root/"tools/materialize.py")
            module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
            private=serialization.load_pem_private_key(key_path.read_bytes(),password=None)
            assert isinstance(private,Ed25519PrivateKey)
            anchor=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
            _der,key_id=module._public_identity(private.public_key())
            module.PRODUCTION_RECEIPT_AUTHORITY_ROOT=target
            module._load_canonical_package_authority=lambda:(private,private.public_key(),anchor,key_id)
            original_write=os.write
            if partial:
                module.os.write=lambda descriptor,raw: original_write(descriptor,raw[:7])
            def checkpoint(candidate):
                if candidate == point:
                    os.kill(os.getpid(),signal.SIGKILL)
            module._bootstrap_checkpoint=checkpoint
            os.umask(0o777)
            result=module.bootstrap_authority(authority_root=os.fspath(target),now=1900000000)
            print(json.dumps(result,sort_keys=True,separators=(",",":")))
            """
        )
        key_path = self.parent / "package-key.pem"
        key_path.write_bytes(
            self.package_private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        os.chmod(key_path, 0o600)
        points = (
            ("after-stage-mkdir", "full"),
            ("private-file:bootstrap-intent.v2.json:after-open", "full"),
            ("private-file:bootstrap-intent.v2.json:after-fchmod", "full"),
            ("private-file:bootstrap-intent.v2.json:after-write", "partial"),
            ("private-file:bootstrap-intent.v2.json:after-fsync", "full"),
            ("private-file:bootstrap-intent.v2.json:after-parent-fsync", "full"),
            ("after-authority-stage-mkdir", "full"),
            ("after-authority-stage-chmod", "full"),
            ("private-file:.receipt-authority-v2.key.pending:after-open", "full"),
            ("private-file:.receipt-authority-v2.key.pending:after-fchmod", "full"),
            ("private-file:.receipt-authority-v2.key.pending:after-write", "partial"),
            ("private-file:.receipt-authority-v2.key.pending:after-fsync", "full"),
            ("private-file:.receipt-authority-v2.key.pending:after-parent-fsync", "full"),
            ("after-key-promote", "full"),
            ("private-file:authority-bootstrap.v2.json:after-write", "partial"),
            ("before-authority-promote", "full"),
            ("after-authority-promote", "full"),
        )
        for index, (point, partial) in enumerate(points):
            with self.subTest(point=point, partial=partial):
                case = self.parent / f"kill-{index}"
                case.mkdir(mode=0o700)
                target = case / "single-host-v2-receipt-authority"
                command = [
                    sys.executable,
                    "-c",
                    helper,
                    os.fspath(MODULE_ROOT),
                    os.fspath(target),
                    os.fspath(key_path),
                    point,
                    partial,
                ]
                killed = subprocess.run(
                    command,
                    cwd=MODULE_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
                completed = subprocess.run(
                    [*command[:-2], "never", "full"],
                    cwd=MODULE_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertRegex(
                    result["receipt_authority_key_id"], r"^sha256:[0-9a-f]{64}$"
                )
                stage = case / ".single-host-v2-receipt-authority.bootstrap.v2"
                self.assertFalse(stage.exists())
                self.assertFalse(
                    any(
                        item.name.endswith(".pending")
                        or ".materializing." in item.name
                        for item in case.rglob("*")
                    )
                )


class RunnerReservationBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-runner-binding-test-"
        )
        self.parent = Path(self.temporary.name)
        os.chmod(self.parent, 0o700)
        self.active = self.parent / "single-host-v2-runner-reservation"
        self.terminal = self.parent / "single-host-v2-runner-reservation-terminal"
        self.approvals = (
            self.parent / "single-host-v2-runner-prerequisite-approvals"
        )
        self.lock = self.parent / ".single-host-v2-runner-reservation.lock"
        self.checkout_root = self.parent / "single-host-v2-release-checkouts"
        self.checkout_root.mkdir(mode=0o700)
        self.receipt_private = Ed25519PrivateKey.generate()
        _der, self.receipt_id = materialize._public_identity(
            self.receipt_private.public_key()
        )
        self.patches = (
            mock.patch.object(materialize, "RUNNER_RESERVATION_PARENT", self.parent),
            mock.patch.object(materialize, "RUNNER_RESERVATION_ROOT", self.active),
            mock.patch.object(materialize, "RUNNER_RESERVATION_LOCK", self.lock),
            mock.patch.object(
                materialize, "RUNNER_RESERVATION_TERMINAL_ROOT", self.terminal
            ),
            mock.patch.object(
                materialize,
                "RUNNER_PREREQUISITE_APPROVAL_ROOT",
                self.approvals,
            ),
            mock.patch.object(
                materialize, "RUNNER_RELEASE_CHECKOUT_ROOT", self.checkout_root
            ),
            mock.patch.object(
                materialize,
                "_verify_runtime_compose_sync",
                return_value={
                    "reservation_sha256": "sha256:" + "1" * 64,
                    "schema": (
                        "propertyquarry.release-control.single-host-runtime-"
                        "compose-sync-verify-result.v2"
                    ),
                    "terminal_sha256": "sha256:" + "2" * 64,
                    "transaction_id": "3" * 64,
                    "version": 2,
                    "workflow_sha": "4" * 40,
                },
            ),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def _write_active(self, raw: bytes) -> None:
        self.active.mkdir(mode=0o700)
        path = self.active / materialize.RUNNER_RESERVATION_NAME
        path.write_bytes(raw)
        os.chmod(path, 0o600)

    @staticmethod
    def _prerequisite_binding() -> dict[str, object]:
        return {
            "runner_prerequisite_approval_payload_sha256": "sha256:" + "d" * 64,
            "runner_prerequisite_approval_sha256": "sha256:" + "e" * 64,
            "runner_prerequisite_intent_sha256": "sha256:" + "f" * 64,
            "runner_prerequisite_job_id": "455",
        }

    def _write_prerequisite_records(
        self, reservation_raw: bytes, reservation_payload: dict[str, object]
    ) -> dict[str, object]:
        self.approvals.mkdir(mode=0o700, exist_ok=True)
        reservation_sha256 = materialize.package.sha256(reservation_raw)
        intent = {
            "authority_profile": materialize.package.PROFILE,
            "comment": "PropertyQuarry governed prerequisite approval "
            + reservation_sha256,
            "discovered_at_epoch": 1_900_000_000,
            "environment_id": "77",
            "environment_name": materialize.RUNNER_PREREQUISITE_ENVIRONMENT,
            "initial_jobs_sha256": "sha256:" + "1" * 64,
            "initial_pending_deployments_sha256": "sha256:" + "2" * 64,
            "initial_runs_index_sha256": "sha256:" + "3" * 64,
            "prerequisite_job_id": "455",
            "prerequisite_job_name": materialize.RUNNER_PREREQUISITE_JOB,
            "receipt_authority_key_id": self.receipt_id,
            "release_job": materialize.package.RELEASE_JOB,
            "repository": materialize.package.REPOSITORY,
            "repository_id": materialize.package.REPOSITORY_ID,
            "repository_owner_id": materialize.package.REPOSITORY_OWNER_ID,
            "reservation_expires_at_epoch": reservation_payload["expires_at_epoch"],
            "reservation_sha256": reservation_sha256,
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": reservation_payload["runner_label"],
            "schema": materialize.RUNNER_PREREQUISITE_INTENT_SCHEMA,
            "version": 2,
            "workflow_path": materialize.SMOKE_WORKFLOW_PATH,
            "workflow_ref": materialize.package.WORKFLOW_REF,
            "workflow_sha": reservation_payload["workflow_sha"],
        }
        intent_raw = materialize._signed_runner_record(
            intent,
            private=self.receipt_private,
            key_id=self.receipt_id,
            domain=materialize.RUNNER_PREREQUISITE_INTENT_SIGNATURE_DOMAIN,
        )
        approval = {
            "approval_api_disposition": "approved",
            "approval_response_sha256": "sha256:" + "4" * 64,
            "approved_at_epoch": 1_900_000_000,
            "completed_jobs_sha256": "sha256:" + "5" * 64,
            "environment_id": intent["environment_id"],
            "environment_name": intent["environment_name"],
            "intent_sha256": materialize.package.sha256(intent_raw),
            "post_pending_deployments_sha256": "sha256:" + "6" * 64,
            "prerequisite_conclusion": "success",
            "prerequisite_job_id": intent["prerequisite_job_id"],
            "prerequisite_job_name": intent["prerequisite_job_name"],
            "receipt_authority_key_id": self.receipt_id,
            "release_job": intent["release_job"],
            "repository": intent["repository"],
            "repository_id": intent["repository_id"],
            "repository_owner_id": intent["repository_owner_id"],
            "reservation_expires_at_epoch": intent["reservation_expires_at_epoch"],
            "reservation_sha256": reservation_sha256,
            "review_history_sha256": "sha256:" + "7" * 64,
            "run_attempt": intent["run_attempt"],
            "run_id": intent["run_id"],
            "runner_label": intent["runner_label"],
            "schema": materialize.RUNNER_PREREQUISITE_APPROVAL_SCHEMA,
            "version": 2,
            "workflow_path": intent["workflow_path"],
            "workflow_ref": intent["workflow_ref"],
            "workflow_sha": intent["workflow_sha"],
        }
        approval_raw = materialize._signed_runner_record(
            approval,
            private=self.receipt_private,
            key_id=self.receipt_id,
            domain=materialize.RUNNER_PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN,
        )
        intent_path, approval_path = materialize._runner_prerequisite_paths(
            reservation_raw
        )
        intent_path.write_bytes(intent_raw)
        approval_path.write_bytes(approval_raw)
        os.chmod(intent_path, 0o600)
        os.chmod(approval_path, 0o600)
        return materialize._validate_runner_prerequisite_records(
            intent_raw=intent_raw,
            approval_raw=approval_raw,
            reservation_raw=reservation_raw,
            reservation_payload=reservation_payload,
            receipt_public=self.receipt_private.public_key(),
            receipt_id=self.receipt_id,
            current=1_900_000_000,
        )

    @staticmethod
    def _evidence() -> dict[str, str]:
        return {
            "envelope_sha": "3" * 64,
            "final_artifact_id": "101",
            "final_artifact_sha256": "4" * 64,
            "github_run_attempt": "1",
            "github_run_completed_at_epoch": "1900000000",
            "github_run_id": "102",
            "preflight_artifact_id": "103",
            "preflight_artifact_sha256": "5" * 64,
            "release_hygiene_sha256": "6" * 64,
            "render_attestation_id": "104",
            "render_image": (
                "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime"
                "@sha256:" + "7" * 64
            ),
            "runtime_sha": "a" * 40,
            "web_attestation_id": "105",
            "web_image": (
                "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime"
                "@sha256:" + "8" * 64
            ),
            "workflow_sha": "b" * 40,
        }

    def _validated(self, raw: bytes, evidence: dict[str, str]) -> dict[str, object]:
        config = {
            "deployment_id": "9" * 64,
            "envelope_sha": evidence["envelope_sha"],
            "release_generation": 1,
            "render_image": evidence["render_image"],
            "runtime_sha": evidence["runtime_sha"],
            "web_image": evidence["web_image"],
            "workflow_sha": evidence["workflow_sha"],
        }
        receipt = {
            "config_sha256": "sha256:" + "1" * 64,
            "final_artifact_id": evidence["final_artifact_id"],
            "final_artifact_sha256": evidence["final_artifact_sha256"],
            "image_publication_run_attempt": evidence["github_run_attempt"],
            "image_publication_run_completed_at_epoch": int(
                evidence["github_run_completed_at_epoch"]
            ),
            "image_publication_run_id": evidence["github_run_id"],
            "plan_sha256": "sha256:" + "2" * 64,
            "preflight_artifact_id": evidence["preflight_artifact_id"],
            "preflight_artifact_sha256": evidence["preflight_artifact_sha256"],
            "release_hygiene_sha256": evidence["release_hygiene_sha256"],
            "render_attestation_id": evidence["render_attestation_id"],
            "runner_launch_ticket_sha256": "sha256:" + "3" * 64,
            "runner_prerequisite_approval_payload_sha256": "sha256:" + "d" * 64,
            "runner_prerequisite_approval_sha256": "sha256:" + "e" * 64,
            "runner_prerequisite_intent_sha256": "sha256:" + "f" * 64,
            "runner_prerequisite_job_id": "455",
            "valid_until_epoch": 1_900_001_800,
            "web_attestation_id": evidence["web_attestation_id"],
        }
        return {
            "config": config,
            "claim_raw": b'{"signed":"claim"}',
            "expected_binding": self._binding_payload(raw),
            "receipt": receipt,
            "reservation_binding": {
                "reservation_sha256": materialize.package.sha256(raw)
            },
            "reservation_raw": raw,
        }

    def _binding_payload(self, raw: bytes) -> dict[str, object]:
        return {
            "authority_profile": materialize.package.PROFILE,
            "bound_at_epoch": 1_900_000_001,
            "claim_sha256": "sha256:" + "1" * 64,
            "config_sha256": "sha256:" + "2" * 64,
            "config_signature_sha256": "sha256:" + "3" * 64,
            "deployment_id": "4" * 64,
            "environment": materialize.package.ENVIRONMENT,
            "job_id": "456",
            "materialization_receipt_sha256": "sha256:" + "5" * 64,
            "materialization_receipt_signature_sha256": "sha256:" + "6" * 64,
            "materialization_root": "/unused-materialization",
            "materialization_root_identity_sha256": "sha256:" + "7" * 64,
            "plan_sha256": "sha256:" + "8" * 64,
            "receipt_authority_key_id": self.receipt_id,
            "reservation_sha256": materialize.package.sha256(raw),
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": "pqrelease-" + "a" * 32,
            "runner_launch_ticket_sha256": "sha256:" + "9" * 64,
            **self._prerequisite_binding(),
            "runtime_sha": "a" * 40,
            "schema": materialize.RUNNER_MATERIALIZATION_BINDING_SCHEMA,
            "version": 2,
            "workflow_sha": "b" * 40,
        }

    def _binding_raw(self, raw: bytes) -> bytes:
        return materialize._signed_runner_record(
            self._binding_payload(raw),
            private=self.receipt_private,
            key_id=self.receipt_id,
            domain=materialize.RUNNER_MATERIALIZATION_BINDING_SIGNATURE_DOMAIN,
        )

    def _consume(self, raw: bytes) -> str:
        return materialize._consume_runner_reservation(
            raw,
            self._binding_raw(raw),
            receipt_public=self.receipt_private.public_key(),
            receipt_id=self.receipt_id,
        )

    def test_pending_claim_is_adopted_without_rotating_start_or_deployment(self) -> None:
        raw = b'{"signed":"reservation-pending"}'
        self._write_active(raw)
        evidence = self._evidence()
        reservation_payload = {
            "created_at_epoch": 1_900_000_000,
            "expires_at_epoch": 1_900_021_600,
            "reservation_nonce": "c" * 64,
        }
        reservation_binding = {
            "reservation_sha256": materialize.package.sha256(raw),
            "runner_label": "pqrelease-" + "a" * 32,
        }
        output = os.fspath(self.parent / "materialization-a")
        point = (
            "runner-terminal:"
            + materialize._runner_claim_path(raw).name
            + ":after-pending-parent-fsync"
        )

        def checkpoint(candidate: str) -> None:
            if candidate == point:
                raise RuntimeError("synthetic-claim-crash")

        with mock.patch.object(
            materialize, "_materialization_checkpoint", checkpoint
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic-claim-crash"):
                materialize._ensure_runner_materialization_claim(
                    reservation_raw=raw,
                    reservation_payload=reservation_payload,
                    reservation_binding=reservation_binding,
                    prerequisite_binding=self._prerequisite_binding(),
                    output=output,
                    release_evidence=evidence,
                    requested_started=1_900_000_000,
                    requested_deployment_id="1" * 64,
                    receipt_private=self.receipt_private,
                    receipt_public=self.receipt_private.public_key(),
                    receipt_id=self.receipt_id,
                    enforce_deadline=True,
                )
        claim_path = materialize._runner_claim_path(raw)
        self.assertFalse(claim_path.exists())
        self.assertTrue(claim_path.with_name(claim_path.name + ".pending").exists())
        payload, _claim_raw, disposition = (
            materialize._ensure_runner_materialization_claim(
                reservation_raw=raw,
                reservation_payload=reservation_payload,
                reservation_binding=reservation_binding,
                prerequisite_binding=self._prerequisite_binding(),
                output=output,
                release_evidence=evidence,
                requested_started=1_900_000_100,
                requested_deployment_id="2" * 64,
                receipt_private=self.receipt_private,
                receipt_public=self.receipt_private.public_key(),
                receipt_id=self.receipt_id,
                enforce_deadline=True,
            )
        )
        self.assertEqual(disposition, "already-claimed")
        self.assertEqual(payload["deployment_id"], "1" * 64)
        self.assertEqual(payload["claimed_at_epoch"], 1_900_000_000)
        self.assertTrue(claim_path.exists())
        self.assertFalse(claim_path.with_name(claim_path.name + ".pending").exists())

    def test_prerequisite_records_reject_tamper_and_reservation_rebind(self) -> None:
        reservation_raw = b'{"signed":"reservation-prerequisite"}'
        reservation_payload = {
            "created_at_epoch": 1_900_000_000,
            "expires_at_epoch": 1_900_021_600,
            "runner_label": "pqrelease-" + "a" * 32,
            "workflow_sha": "b" * 40,
        }
        binding = self._write_prerequisite_records(
            reservation_raw, reservation_payload
        )
        intent_raw, approval_raw = materialize._read_runner_prerequisite_records(
            reservation_raw
        )
        self.assertEqual(
            binding["runner_prerequisite_approval_sha256"],
            materialize.package.sha256(approval_raw),
        )
        tampered = bytearray(approval_raw)
        tampered[-2] ^= 1
        with self.assertRaisesRegex(
            materialize.MaterializeFailure,
            "runner-prerequisite-approval-(?:wire|signature|encoding)-invalid",
        ):
            materialize._validate_runner_prerequisite_records(
                intent_raw=intent_raw,
                approval_raw=bytes(tampered),
                reservation_raw=reservation_raw,
                reservation_payload=reservation_payload,
                receipt_public=self.receipt_private.public_key(),
                receipt_id=self.receipt_id,
                current=1_900_000_000,
            )
        with self.assertRaisesRegex(
            materialize.MaterializeFailure,
            "runner-prerequisite-(?:intent|approval)-binding-invalid",
        ):
            materialize._validate_runner_prerequisite_records(
                intent_raw=intent_raw,
                approval_raw=approval_raw,
                reservation_raw=b'{"signed":"different-reservation"}',
                reservation_payload=reservation_payload,
                receipt_public=self.receipt_private.public_key(),
                receipt_id=self.receipt_id,
                current=1_900_000_000,
            )

    def test_runner_observation_cannot_overwrite_prerequisite_run_or_job(self) -> None:
        prerequisite = {
            "run_attempt": 2,
            "run_id": "123",
            "runner_prerequisite_job_id": "455",
        }
        valid = {"job_id": "456", "run_attempt": 2, "run_id": "123"}
        self.assertIs(
            materialize._validate_runner_observation_prerequisite(
                valid, prerequisite
            ),
            valid,
        )
        for rebound in (
            {**valid, "run_id": "124"},
            {**valid, "run_attempt": 3},
            {**valid, "job_id": "455"},
        ):
            with self.assertRaisesRegex(
                materialize.MaterializeFailure,
                "runner-observation-prerequisite-binding-invalid",
            ):
                materialize._validate_runner_observation_prerequisite(
                    rebound, prerequisite
                )
    def test_publish_a_crash_rejects_output_b_then_only_a_binds(self) -> None:
        reservation_raw = b'{"signed":"reservation-cross-output"}'
        self._write_active(reservation_raw)
        evidence = self._evidence()
        output_a = self.parent / "materialization-a"
        output_b = self.parent / "materialization-b"
        package_private = Ed25519PrivateKey.generate()
        _der, package_id = materialize._public_identity(
            package_private.public_key()
        )
        authority = (
            b"",
            package_private,
            package_private.public_key(),
            self.receipt_private,
            self.receipt_private.public_key(),
            package_id,
            self.receipt_id,
        )
        reservation_payload = {
            "created_at_epoch": 1_900_000_000,
            "expires_at_epoch": 1_900_021_600,
            "reservation_nonce": "c" * 64,
            "runner_label_nonce": "a" * 32,
            "runner_label": "pqrelease-" + "a" * 32,
            "workflow_sha": evidence["workflow_sha"],
        }
        prerequisite_binding = self._write_prerequisite_records(
            reservation_raw, reservation_payload
        )
        reservation_binding = {
            "reservation_nonce": reservation_payload["reservation_nonce"],
            "reservation_sha256": materialize.package.sha256(reservation_raw),
            "runner_label": "pqrelease-" + "a" * 32,
            "source_checkout_identity_sha256": "sha256:" + "b" * 64,
            "source_checkout_path": "/private/checkout/" + "b" * 40,
            "source_tree_sha256": "sha256:" + "c" * 64,
        }
        runner_observation = {
            "docker_socket": {
                "device": 11,
                "gid": 112,
                "inode": 12,
                "mode": "0660",
                "nlink": 1,
                "path": "/var/run/docker.sock",
                "uid": 0,
            },
            "job_id": "456",
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": reservation_binding["runner_label"],
        }
        observer_calls: list[str] = []

        def observer(_release: dict[str, str], deployment_id: str):
            observer_calls.append(deployment_id)
            return {"observed_at_epoch": 1_900_000_000}

        def build_documents(**values):
            runner = values["runner_binding"]
            plan = {
                "runner_job_id": runner["job_id"],
                "runner_label": runner["runner_label"],
                "runner_prerequisite_approval_payload_sha256": runner[
                    "runner_prerequisite_approval_payload_sha256"
                ],
                "runner_prerequisite_approval_sha256": runner[
                    "runner_prerequisite_approval_sha256"
                ],
                "runner_prerequisite_intent_sha256": runner[
                    "runner_prerequisite_intent_sha256"
                ],
                "runner_prerequisite_job_id": runner[
                    "runner_prerequisite_job_id"
                ],
                "runner_reservation_sha256": runner["reservation_sha256"],
                "runner_run_attempt": runner["run_attempt"],
                "runner_run_id": runner["run_id"],
                "runtime_sha": values["runtime_sha"],
            }
            plan_raw = materialize.package.canonical_json(plan)
            config = {
                "deployment_id": values["deployment_id"],
                "envelope_sha": values["envelope_sha"],
                "plan_digest": materialize.package.sha256(plan_raw),
                "predecessor_runtime_sha": "genesis",
                "release_generation": 1,
                "render_image": values["render_image"],
                "runner_job_id": runner["job_id"],
                "runner_label": runner["runner_label"],
                "runner_prerequisite_approval_payload_sha256": runner[
                    "runner_prerequisite_approval_payload_sha256"
                ],
                "runner_prerequisite_approval_sha256": runner[
                    "runner_prerequisite_approval_sha256"
                ],
                "runner_prerequisite_intent_sha256": runner[
                    "runner_prerequisite_intent_sha256"
                ],
                "runner_prerequisite_job_id": runner[
                    "runner_prerequisite_job_id"
                ],
                "runner_reservation_sha256": runner["reservation_sha256"],
                "runner_run_attempt": runner["run_attempt"],
                "runner_run_id": runner["run_id"],
                "runtime_sha": values["runtime_sha"],
                "transaction_started_at_epoch": values["started"],
                "web_image": values["web_image"],
                "workflow_sha": values["workflow_sha"],
            }
            return config, plan, plan_raw

        crashed = False

        def checkpoint(candidate: str) -> None:
            nonlocal crashed
            if candidate == "after-materialization-publish" and not crashed:
                crashed = True
                raise RuntimeError("synthetic-after-publish-crash")

        patches = (
            mock.patch.object(materialize, "_load_authority", return_value=authority),
            mock.patch.object(
                materialize,
                "_validate_runner_reservation",
                return_value=(reservation_payload, dict(reservation_binding)),
            ),
            mock.patch.object(materialize, "_build_documents", build_documents),
            mock.patch.object(
                materialize.package, "validate_config_and_plan", return_value=None
            ),
            mock.patch.object(materialize, "_materialization_checkpoint", checkpoint),
        )
        for patcher in patches:
            patcher.start()
        try:
            with self.assertRaisesRegex(
                RuntimeError, "synthetic-after-publish-crash"
            ):
                materialize.materialize(
                    authority_root="/unused-authority",
                    output=os.fspath(output_a),
                    release_evidence=evidence,
                    now=1_900_000_000,
                    observer=observer,
                    runner_observer=lambda _reservation, _prerequisite: dict(
                        runner_observation
                    ),
                )
            self.assertTrue(output_a.exists())
            self.assertTrue(self.active.exists())
            calls_after_a = len(observer_calls)
            with self.assertRaisesRegex(
                materialize.MaterializeFailure,
                "runner-materialization-claim-conflict",
            ):
                materialize.materialize(
                    authority_root="/unused-authority",
                    output=os.fspath(output_b),
                    release_evidence=evidence,
                    now=1_900_000_001,
                    observer=observer,
                    runner_observer=lambda _reservation, _prerequisite: dict(
                        runner_observation
                    ),
                )
            self.assertEqual(len(observer_calls), calls_after_a)
            self.assertFalse(output_b.exists())
            rebound = dict(evidence)
            rebound["final_artifact_id"] = "999"
            with self.assertRaisesRegex(
                materialize.MaterializeFailure,
                "runner-materialization-claim-conflict",
            ):
                materialize._ensure_runner_materialization_claim(
                    reservation_raw=reservation_raw,
                    reservation_payload=reservation_payload,
                    reservation_binding=reservation_binding,
                    prerequisite_binding=prerequisite_binding,
                    output=os.fspath(output_a),
                    release_evidence=rebound,
                    requested_started=1_900_000_001,
                    requested_deployment_id="f" * 64,
                    receipt_private=self.receipt_private,
                    receipt_public=self.receipt_private.public_key(),
                    receipt_id=self.receipt_id,
                    enforce_deadline=True,
                )

            files = materialize._read_exact_private_directory(
                os.fspath(output_a), materialize.MATERIAL_FILES
            )
            config = materialize.package.parse_strict_json(
                files["authority.v2.json"], "test-config"
            )
            receipt = materialize.package.parse_strict_json(
                files["materialization-receipt.v2.json"], "test-receipt"
            )
            claim_raw = materialize._read_runner_terminal_file(
                materialize._runner_claim_path(reservation_raw)
            )
            ticket_wire = materialize.package.parse_strict_json(
                files["runner-launch-ticket.v2.json"], "test-ticket"
            )
            expected_binding = materialize._runner_materialization_binding_payload(
                materialization_root=os.fspath(output_a),
                claim_raw=claim_raw,
                reservation_raw=reservation_raw,
                config=config,
                config_raw=files["authority.v2.json"],
                config_signature=files["authority.v2.sig"],
                plan_raw=files["transaction-plan.v2.json"],
                materialization_receipt_raw=files[
                    "materialization-receipt.v2.json"
                ],
                materialization_receipt_signature=files[
                    "materialization-receipt.v2.sig"
                ],
                runner_ticket_raw=files["runner-launch-ticket.v2.json"],
                bound_at=ticket_wire["payload"]["bound_at_epoch"],
                receipt_id=self.receipt_id,
            )
            validated = {
                "claim_raw": claim_raw,
                "config": config,
                "expected_binding": expected_binding,
                "receipt": receipt,
                "reservation_binding": reservation_binding,
                "reservation_raw": reservation_raw,
            }
            with mock.patch.object(
                materialize, "_validated_materialization", return_value=validated
            ) as recovered_validation:
                result = materialize.materialize(
                    authority_root="/unused-authority",
                    output=os.fspath(output_a),
                    release_evidence=evidence,
                    now=1_900_000_001,
                    observer=lambda *_args: self.fail("recovery re-observed live state"),
                    runner_observer=lambda *_args: self.fail(
                        "recovery re-observed runner state"
                    ),
                )
            self.assertEqual(result["runner_reservation_disposition"], "bound")
            self.assertFalse(self.active.exists())
            self.assertFalse(
                recovered_validation.call_args.kwargs["require_bound_terminal"]
            )
            terminal_raw = materialize._read_runner_terminal_file(
                materialize._runner_terminal_path(reservation_raw)
            )
            terminal_payload = materialize._validate_runner_materialization_binding(
                terminal_raw,
                receipt_public=self.receipt_private.public_key(),
                receipt_id=self.receipt_id,
            )
            self.assertEqual(terminal_payload, expected_binding)
            plan = materialize.package.parse_strict_json(
                files["transaction-plan.v2.json"], "test-plan"
            )
            with mock.patch.object(
                materialize.package,
                "validate_config_and_plan",
                return_value=(config, plan, self.receipt_id),
            ), mock.patch.object(
                materialize,
                "_validate_runner_launch_ticket",
                return_value=ticket_wire["payload"],
            ):
                fully_validated = materialize._validated_materialization(
                    authority_root="/unused-authority",
                    materialization_root=os.fspath(output_a),
                    current=1_900_000_001,
                    require_bound_terminal=True,
                    observe_socket=False,
                )
            self.assertEqual(fully_validated["terminal_raw"], terminal_raw)
            self.assertEqual(fully_validated["expected_binding"], terminal_payload)

            copied = self.parent / "materialization-copy"
            shutil.copytree(output_a, copied)
            self.assertNotEqual(
                terminal_payload["materialization_root"], os.fspath(copied)
            )
            self.assertNotEqual(
                terminal_payload["materialization_root_identity_sha256"],
                materialize._runner_output_identity(copied),
            )
            with mock.patch.object(
                materialize.package,
                "validate_config_and_plan",
                return_value=(config, plan, self.receipt_id),
            ), mock.patch.object(
                materialize,
                "_validate_runner_launch_ticket",
                return_value=ticket_wire["payload"],
            ):
                with self.assertRaisesRegex(
                    materialize.MaterializeFailure,
                    "materialization-runner-claim-binding-invalid",
                ):
                    materialize._validated_materialization(
                        authority_root="/unused-authority",
                        materialization_root=os.fspath(copied),
                        current=1_900_000_001,
                        require_bound_terminal=True,
                        observe_socket=False,
                    )
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def test_binding_is_idempotent_and_converges_identical_duplicate(self) -> None:
        raw = b'{"signed":"reservation-a"}'
        self._write_active(raw)
        self.assertEqual(self._consume(raw), "bound")
        self.assertFalse(self.active.exists())

    def test_pending_bound_record_promotes_and_converges_after_crash(self) -> None:
        raw = b'{"signed":"reservation-bound-pending"}'
        self._write_active(raw)
        point = (
            "runner-terminal:"
            + materialize._runner_terminal_path(raw).name
            + ":after-pending-parent-fsync"
        )

        def checkpoint(candidate: str) -> None:
            if candidate == point:
                raise RuntimeError("synthetic-bound-crash")

        with mock.patch.object(
            materialize, "_materialization_checkpoint", checkpoint
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic-bound-crash"):
                self._consume(raw)
        terminal = materialize._runner_terminal_path(raw)
        self.assertFalse(terminal.exists())
        self.assertTrue(terminal.with_name(terminal.name + ".pending").exists())
        self.assertTrue(self.active.exists())
        self.assertEqual(self._consume(raw), "bound")
        self.assertTrue(terminal.exists())
        self.assertFalse(terminal.with_name(terminal.name + ".pending").exists())
        self.assertFalse(self.active.exists())

    def test_terminal_first_active_unlink_crash_converges(self) -> None:
        raw = b'{"signed":"reservation-active-unlink"}'
        self._write_active(raw)

        def checkpoint(candidate: str) -> None:
            if candidate == "runner-reservation:after-active-file-unlink":
                raise RuntimeError("synthetic-active-unlink-crash")

        with mock.patch.object(
            materialize, "_materialization_checkpoint", checkpoint
        ):
            with self.assertRaisesRegex(
                RuntimeError, "synthetic-active-unlink-crash"
            ):
                self._consume(raw)
        self.assertTrue(materialize._runner_terminal_path(raw).exists())
        self.assertTrue(self.active.is_dir())
        self.assertEqual(list(self.active.iterdir()), [])
        self.assertEqual(
            self._consume(raw), "active-bound-duplicate-converged"
        )
        self.assertFalse(self.active.exists())
        self.assertEqual(self._consume(raw), "already-bound")
        self._write_active(raw)
        self.assertEqual(
            self._consume(raw),
            "active-bound-duplicate-converged",
        )
        self.assertFalse(self.active.exists())

    def test_bound_terminal_rejects_conflicting_active_reservation(self) -> None:
        raw = b'{"signed":"reservation-a"}'
        self._write_active(raw)
        self._consume(raw)
        self._write_active(b'{"signed":"reservation-b"}')
        with self.assertRaisesRegex(
            materialize.MaterializeFailure, "runner-reservation-active-bound-conflict"
        ):
            self._consume(raw)
        self.assertTrue(self.active.exists())

    def test_published_materialization_recovers_without_observing_live_state(self) -> None:
        raw = b'{"signed":"reservation-c"}'
        self._write_active(raw)
        evidence = self._evidence()
        authority = (
            b"",
            None,
            None,
            self.receipt_private,
            self.receipt_private.public_key(),
            "sha256:" + "f" * 64,
            self.receipt_id,
        )
        with mock.patch.object(
            materialize,
            "_validated_materialization",
            return_value=self._validated(raw, evidence),
        ) as validate, mock.patch.object(
            materialize, "_load_authority", return_value=authority
        ):
            result = materialize._recover_published_materialization(
                authority_root="/unused-authority",
                output="/unused-materialization",
                release_evidence=evidence,
                now=1_900_000_001,
            )
        self.assertEqual(result["runner_reservation_disposition"], "bound")
        self.assertFalse(self.active.exists())
        self.assertTrue(materialize._runner_terminal_path(raw).exists())
        self.assertFalse(validate.call_args.kwargs["observe_socket"])
        self.assertFalse(validate.call_args.kwargs["require_bound_terminal"])

    def test_published_materialization_conflict_does_not_consume_active(self) -> None:
        raw = b'{"signed":"reservation-d"}'
        self._write_active(raw)
        evidence = self._evidence()
        conflicting = dict(evidence)
        conflicting["final_artifact_id"] = "999"
        with mock.patch.object(
            materialize,
            "_validated_materialization",
            return_value=self._validated(raw, evidence),
        ):
            with self.assertRaisesRegex(
                materialize.MaterializeFailure,
                "published-materialization-release-evidence-conflict",
            ):
                materialize._recover_published_materialization(
                    authority_root="/unused-authority",
                    output="/unused-materialization",
                    release_evidence=conflicting,
                    now=1_900_000_001,
                )
        self.assertTrue(self.active.exists())
        self.assertFalse(self.terminal.exists())


class MaterializeVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-materialize-verifier-test-"
        )
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_failure(self, code: str, callback) -> None:
        with self.assertRaises(materialize.MaterializeFailure) as caught:
            callback()
        self.assertEqual(str(caught.exception), code)

    @staticmethod
    def _snappy_literal(raw: bytes) -> bytes:
        declared = len(raw)
        prefix = bytearray()
        while True:
            value = declared & 0x7F
            declared >>= 7
            prefix.append(value | (0x80 if declared else 0))
            if not declared:
                break
        if len(raw) <= 60:
            return bytes(prefix) + bytes([(len(raw) - 1) << 2]) + raw
        width = max(1, ((len(raw) - 1).bit_length() + 7) // 8)
        return (
            bytes(prefix)
            + bytes([(59 + width) << 2])
            + (len(raw) - 1).to_bytes(width, "little")
            + raw
        )

    @staticmethod
    def _der_utf8(value: str) -> bytes:
        raw = value.encode("utf-8")
        if len(raw) < 128:
            return b"\x0c" + bytes([len(raw)]) + raw
        width = (len(raw).bit_length() + 7) // 8
        return b"\x0c" + bytes([0x80 | width]) + len(raw).to_bytes(width, "big") + raw

    def _certificate_bundle(
        self, *, workflow_sha: str, run_id: str, run_attempt: str, subject: str | None = None
    ) -> tuple[dict[str, object], bytes]:
        private = Ed25519PrivateKey.generate()
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")]))
            .public_key(private.public_key())
            .serial_number(1)
            .not_valid_before(now.replace(year=now.year - 1))
            .not_valid_after(now.replace(year=now.year + 1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(materialize.PUBLISH_WORKFLOW_IDENTITY)]
                ),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]),
                critical=False,
            )
        )
        plain = {
            1: "https://token.actions.githubusercontent.com",
            2: "workflow_dispatch",
            3: workflow_sha,
            4: "propertyquarry-publish-runtime-images",
            5: materialize.package.REPOSITORY,
            6: "refs/heads/main",
        }
        wrapped = {
            8: "https://token.actions.githubusercontent.com",
            9: materialize.PUBLISH_WORKFLOW_IDENTITY,
            10: workflow_sha,
            11: "github-hosted",
            12: "https://github.com/ArchonMegalon/propertyquarry",
            13: workflow_sha,
            14: "refs/heads/main",
            15: materialize.package.REPOSITORY_ID,
            16: "https://github.com/ArchonMegalon",
            17: materialize.package.REPOSITORY_OWNER_ID,
            18: materialize.PUBLISH_WORKFLOW_IDENTITY,
            19: workflow_sha,
            20: "workflow_dispatch",
            21: f"https://github.com/{materialize.package.REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}",
            22: "public",
            23: materialize.package.ENVIRONMENT,
            24: subject
            or "repo:ArchonMegalon@11421547/propertyquarry@1257593732:environment:propertyquarry-production",
        }
        for suffix, value in plain.items():
            builder = builder.add_extension(
                x509.UnrecognizedExtension(
                    ObjectIdentifier(f"1.3.6.1.4.1.57264.1.{suffix}"),
                    value.encode("utf-8"),
                ),
                critical=False,
            )
        for suffix, value in wrapped.items():
            builder = builder.add_extension(
                x509.UnrecognizedExtension(
                    ObjectIdentifier(f"1.3.6.1.4.1.57264.1.{suffix}"),
                    self._der_utf8(value),
                ),
                critical=False,
            )
        certificate = builder.sign(private, algorithm=None)
        certificate_raw = certificate.public_bytes(serialization.Encoding.DER)
        bundle = {
            "verificationMaterial": {
                "certificate": {
                    "rawBytes": base64.b64encode(certificate_raw).decode("ascii")
                }
            }
        }
        return bundle, certificate_raw

    def _write_zip(self, name: str, members: list[tuple[zipfile.ZipInfo, bytes]]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for info, raw in members:
                archive.writestr(info, raw)
        os.chmod(path, 0o600)
        return path

    def test_raw_snappy_accepts_literal_and_copy_and_rejects_adversarial_streams(self) -> None:
        self.assertEqual(
            materialize._snappy_raw_decode(self._snappy_literal(b"hello")), b"hello"
        )
        copied = b"\x08\x0cabcd\x0e\x04\x00"
        self.assertEqual(materialize._snappy_raw_decode(copied), b"abcdabcd")
        for raw, code in (
            (self._snappy_literal(b"hello") + b"\x00", "sigstore-remote-snappy-trailing-or-size-invalid"),
            (b"\x04\x0e\x00\x00", "sigstore-remote-snappy-copy-invalid"),
            (b"\x00", "sigstore-remote-snappy-size-invalid"),
            (b"\x80", "sigstore-remote-snappy-invalid"),
        ):
            with self.subTest(raw=raw):
                self.assert_failure(code, lambda raw=raw: materialize._snappy_raw_decode(raw))

    def test_der_utf8_and_immutable_oid_subject_are_exact(self) -> None:
        self.assertEqual(materialize._decode_der_utf8(b"\x0c\x03abc"), "abc")
        for raw in (b"\x13\x01a", b"\x0c\x81\x01a", b"\x0c\x82\x00\x80" + b"a" * 128):
            self.assert_failure(
                "sigstore-certificate-extension-invalid",
                lambda raw=raw: materialize._decode_der_utf8(raw),
            )
        workflow_sha = "a" * 40
        valid, _raw = self._certificate_bundle(
            workflow_sha=workflow_sha, run_id="123", run_attempt="1"
        )
        materialize._verify_certificate_claims(
            valid, workflow_sha=workflow_sha, run_id="123", run_attempt="1"
        )
        legacy, _raw = self._certificate_bundle(
            workflow_sha=workflow_sha,
            run_id="123",
            run_attempt="1",
            subject="repo:ArchonMegalon/propertyquarry:environment:propertyquarry-production",
        )
        self.assert_failure(
            "sigstore-certificate-identity-invalid",
            lambda: materialize._verify_certificate_claims(
                legacy, workflow_sha=workflow_sha, run_id="123", run_attempt="1"
            ),
        )

    def test_release_zip_requires_exact_regular_nonempty_members(self) -> None:
        names = frozenset({"one.json", "two.json"})
        valid_members = [(zipfile.ZipInfo(name), b"{}") for name in sorted(names)]
        valid = self._write_zip("valid.zip", valid_members)
        _archive, members = materialize._read_release_zip(os.fspath(valid), names)
        self.assertEqual(members, {"one.json": b"{}", "two.json": b"{}"})
        duplicate_info = zipfile.ZipInfo("one.json")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            duplicate = self._write_zip(
                "duplicate.zip",
                [(duplicate_info, b"{}"), (zipfile.ZipInfo("one.json"), b"{}")],
            )
        symlink = zipfile.ZipInfo("one.json")
        symlink.create_system = 3
        symlink.external_attr = (0o120777 << 16)
        malicious = (
            (duplicate, "release-artifact-member-set-invalid", names),
            (
                self._write_zip(
                    "symlink.zip",
                    [(symlink, b"target"), (zipfile.ZipInfo("two.json"), b"{}")],
                ),
                "release-artifact-member-invalid",
                names,
            ),
            (
                self._write_zip(
                    "empty.zip",
                    [(zipfile.ZipInfo("one.json"), b""), (zipfile.ZipInfo("two.json"), b"{}")],
                ),
                "release-artifact-member-invalid",
                names,
            ),
        )
        for path, code, expected in malicious:
            with self.subTest(path=path.name):
                self.assert_failure(
                    code,
                    lambda path=path, expected=expected: materialize._read_release_zip(
                        os.fspath(path), expected
                    ),
                )

    def test_numeric_ids_reject_zero_padding_unicode_and_unbounded_values(self) -> None:
        for valid in ("1", "9", "12345678901234567890"):
            self.assertTrue(materialize._numeric_id(valid))
        for invalid in ("", "0", "01", "+1", "１２", "1" * 21, 1, None):
            self.assertFalse(materialize._numeric_id(invalid))

    def test_bounded_process_caps_streams_and_kills_timeout_group(self) -> None:
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        completed = materialize._run_bounded_process(
            ["/usr/bin/python3", "-c", "print('ok')"],
            cwd=os.fspath(self.root),
            env=environment,
            timeout=5,
            stdout_limit=16,
            stderr_limit=16,
            code="bounded-failed",
        )
        self.assertEqual(completed.stdout, b"ok\n")
        self.assert_failure(
            "bounded-failed",
            lambda: materialize._run_bounded_process(
                ["/usr/bin/python3", "-c", "print('x'*1000)"],
                cwd=os.fspath(self.root), env=environment, timeout=5,
                stdout_limit=16, stderr_limit=16, code="bounded-failed",
            ),
        )

    def test_sigstore_verifier_argv_environment_and_root_recheck_are_exact(self) -> None:
        workflow_sha = "a" * 40
        run_id, run_attempt = "123", "1"
        repository = "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime"
        digest_value = "sha256:" + "b" * 64
        statement = materialize._expected_slsa_statement(
            repository, digest_value, workflow_sha, run_id, run_attempt
        )
        certificate_bundle, _raw = self._certificate_bundle(
            workflow_sha=workflow_sha, run_id=run_id, run_attempt=run_attempt
        )
        bundle = {
            "dsseEnvelope": {
                "payload": base64.b64encode(
                    json.dumps(
                        statement, sort_keys=True, separators=(",", ":")
                    ).encode("ascii")
                ).decode("ascii"),
                "payloadType": "application/vnd.in-toto+json",
                "signatures": [{"sig": "fixture"}],
            },
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "verificationMaterial": certificate_bundle["verificationMaterial"],
        }
        expected_certificate = {
            "certificateIssuer": "CN=sigstore-intermediate,O=sigstore.dev",
            "subjectAlternativeName": materialize.PUBLISH_WORKFLOW_IDENTITY,
            "issuer": "https://token.actions.githubusercontent.com",
            "githubWorkflowTrigger": "workflow_dispatch",
            "githubWorkflowSHA": workflow_sha,
            "githubWorkflowName": "propertyquarry-publish-runtime-images",
            "githubWorkflowRepository": materialize.package.REPOSITORY,
            "githubWorkflowRef": "refs/heads/main",
            "buildSignerURI": materialize.PUBLISH_WORKFLOW_IDENTITY,
            "buildSignerDigest": workflow_sha,
            "runnerEnvironment": "github-hosted",
            "sourceRepositoryURI": "https://github.com/ArchonMegalon/propertyquarry",
            "sourceRepositoryDigest": workflow_sha,
            "sourceRepositoryRef": "refs/heads/main",
            "sourceRepositoryIdentifier": materialize.package.REPOSITORY_ID,
            "sourceRepositoryOwnerURI": "https://github.com/ArchonMegalon",
            "sourceRepositoryOwnerIdentifier": materialize.package.REPOSITORY_OWNER_ID,
            "buildConfigURI": materialize.PUBLISH_WORKFLOW_IDENTITY,
            "buildConfigDigest": workflow_sha,
            "buildTrigger": "workflow_dispatch",
            "runInvocationURI": f"https://github.com/{materialize.package.REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}",
            "sourceRepositoryVisibilityAtSigning": "public",
        }
        timestamp = datetime.fromtimestamp(1_900_000_010, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        verifier_output = json.dumps(
            [
                {
                    "attestation": {
                        "bundle": bundle,
                        "bundle_url": "",
                        "initiator": "",
                    },
                    "verificationResult": {
                        "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
                        "signature": {"certificate": expected_certificate},
                        "statement": statement,
                        "verifiedTimestamps": [
                            {
                                "timestamp": timestamp,
                                "type": "Tlog",
                                "uri": "https://rekor.sigstore.dev",
                            }
                        ],
                    },
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        verifier = self.root / "verifier" / "gh"
        trusted = verifier.parent / "trusted_root.jsonl"
        captured: dict[str, object] = {}

        def run(argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, verifier_output, b"")

        raw = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
        with mock.patch.object(materialize, "_run_bounded_process", run), mock.patch.object(
            materialize,
            "_load_attestation_verifier",
            return_value=(verifier, trusted),
        ):
            observed = materialize._verify_sigstore_bundle(
                raw,
                repository=repository,
                digest_value=digest_value,
                workflow_sha=workflow_sha,
                run_id=run_id,
                run_attempt=run_attempt,
                run_started_at=1_900_000_000,
                run_completed_at=1_900_000_100,
                verifier_binary=verifier,
                trusted_root=trusted,
            )
        self.assertEqual(observed, bundle)
        argv = captured["argv"]
        self.assertEqual(argv[0:4], [os.fspath(verifier), "attestation", "verify", f"oci://{repository}@{digest_value}"])
        for flag in (
            "--bundle", "--custom-trusted-root", "--repo", "--cert-identity",
            "--cert-oidc-issuer", "--signer-digest", "--source-digest",
            "--source-ref", "--deny-self-hosted-runners", "--predicate-type", "--format",
        ):
            self.assertEqual(argv.count(flag), 1)
        environment = captured["env"]
        self.assertEqual(
            set(environment),
            {
                "DO_NOT_TRACK", "GH_CONFIG_DIR", "GH_NO_UPDATE_NOTIFIER",
                "GH_PROMPT_DISABLED", "GH_SPINNER_DISABLED", "GH_TELEMETRY",
                "HOME", "LANG", "LC_ALL", "NO_COLOR", "PATH", "TZ", "XDG_CACHE_HOME",
            },
        )
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("HTTPS_PROXY", environment)
        with mock.patch.object(materialize, "_run_bounded_process", run), mock.patch.object(
            materialize,
            "_load_attestation_verifier",
            return_value=(verifier, trusted.with_name("mutated.jsonl")),
        ):
            self.assert_failure(
                "attestation-verifier-mutated",
                lambda: materialize._verify_sigstore_bundle(
                    raw,
                    repository=repository,
                    digest_value=digest_value,
                    workflow_sha=workflow_sha,
                    run_id=run_id,
                    run_attempt=run_attempt,
                    run_started_at=1_900_000_000,
                    run_completed_at=1_900_000_100,
                    verifier_binary=verifier,
                    trusted_root=trusted,
                ),
            )
        self.assert_failure(
            "bounded-failed",
            lambda: materialize._run_bounded_process(
                ["/usr/bin/python3", "-c", "import time;time.sleep(5)"],
                cwd=os.fspath(self.root), env=environment, timeout=1,
                stdout_limit=16, stderr_limit=16, code="bounded-failed",
            ),
        )


if __name__ == "__main__":
    unittest.main()
