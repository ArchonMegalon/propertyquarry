from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import unittest

from tests import test_package


package = test_package.package


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "tools" / "verify-install-receipt.py"
DOMAIN = (
    b"propertyquarry.release-control.single-host-install-receipt-signature.v2\x00"
)
CANARY_DOMAIN = (
    b"propertyquarry.release-control.single-host-activation-canary-"
    b"receipt-signature.v2\x00"
)


class ReceiptVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_package.PackageTests(
            methodName=(
                "test_deterministic_signed_package_verifies_and_stages_non_authoritatively"
            )
        )
        self.fixture.setUp()
        self.archive = self.fixture._build("receipt-package.tar")
        self.verified = package.verify_package(
            os.fspath(self.archive), os.fspath(self.fixture.paths["package_public"])
        )
        self.build = package.parse_strict_json(
            self.verified.members[
                "payload/etc/propertyquarry-release-single-host-v2/"
                "native-build-receipt.v2.json"
            ],
            "test-build-receipt",
            trailing_newline=True,
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _receipt(self, name: str, payload: dict[str, object]) -> Path:
        payload_raw = package.canonical_json(payload)
        signature = self.fixture.receipt_private.sign(
            package.framed(DOMAIN, payload_raw)
        )
        wrapper = {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode(
                "ascii"
            ),
            "signature_key_id": self.verified.manifest[
                "receipt_authority_key_id"
            ],
        }
        return self.fixture._write(name, package.canonical_json(wrapper), 0o600)

    def _canary(self, verified_at: int = 1) -> tuple[dict[str, object], str]:
        manifest = self.verified.manifest
        files = {item["install_path"]: item for item in manifest["files"]}
        challenge = "sha256:" + "c" * 64
        payload: dict[str, object] = {
            "authority_profile": package.PROFILE,
            "challenge_sha256": challenge,
            "config_digest": manifest["config_digest"],
            "controller_sha256": files[
                "/usr/libexec/propertyquarry-release-control/"
                "propertyquarry-release-single-host-v2"
            ]["sha256"],
            "github_immutable_oidc_subject_verified": True,
            "github_repository_runner_admin_read_verified": True,
            "immutable_subject": (
                "repo:ArchonMegalon@11421547/propertyquarry@1257593732:"
                "environment:propertyquarry-production"
            ),
            "package_authority_key_id": manifest["package_authority_key_id"],
            "package_manifest_digest": package.sha256(
                self.verified.members["manifest.v2.json"]
            ),
            "plan_digest": manifest["plan_digest"],
            "receipt_authority_key_id": manifest["receipt_authority_key_id"],
            "repository": package.REPOSITORY,
            "repository_id": package.REPOSITORY_ID,
            "repository_owner_id": package.REPOSITORY_OWNER_ID,
            "runtime_sha": manifest["runtime_sha"],
            "schema": (
                "propertyquarry.release-control.single-host-"
                "activation-canary-receipt.v2"
            ),
            "unit_sha256": files[
                "/usr/lib/systemd/system/"
                "propertyquarry-release-single-host-v2-activation-canary.service"
            ]["sha256"],
            "valid_until": verified_at + 120,
            "verified_at": verified_at,
            "version": 2,
            "workflow_sha": manifest["workflow_sha"],
        }
        signature = self.fixture.receipt_private.sign(
            package.framed(CANARY_DOMAIN, package.canonical_json(payload))
        )
        wrapper: dict[str, object] = {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .rstrip(b"=")
            .decode("ascii"),
            "signature_key_id": manifest["receipt_authority_key_id"],
        }
        return wrapper, package.sha256(package.canonical_json(wrapper))

    def _run(self, kind: str, receipt: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                os.fspath(VERIFIER),
                "--kind",
                kind,
                "--package",
                os.fspath(self.archive),
                "--package-authority-public-key",
                os.fspath(self.fixture.paths["package_public"]),
                "--receipt",
                os.fspath(receipt),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_signed_success_install_receipt_is_fully_bound(self) -> None:
        manifest = self.verified.manifest
        canary, canary_digest = self._canary()
        canary_payload = canary["payload"]
        assert isinstance(canary_payload, dict)
        payload: dict[str, object] = {
            "activation_canary_challenge_sha256": canary_payload[
                "challenge_sha256"
            ],
            "activation_canary_receipt": canary,
            "activation_canary_receipt_digest": canary_digest,
            "activation_canary_unit_sha256": canary_payload["unit_sha256"],
            "activation_canary_valid_until": canary_payload["valid_until"],
            "activation_canary_verified": True,
            "activation_canary_verified_at": canary_payload["verified_at"],
            "activation_performed": True,
            "activation_succeeded": True,
            "archive_digest": self.verified.archive_sha256,
            "authority_installed": True,
            "authority_profile": package.PROFILE,
            "backup_encryption_key_created": True,
            "backup_encryption_key_id": "sha256:" + "b" * 64,
            "candidate_authority_installed": True,
            "config_digest": manifest["config_digest"],
            "deactivation_performed": False,
            "deactivation_succeeded": False,
            "disposition": "installed-and-active",
            "envelope_sha": manifest["envelope_sha"],
            "host_machine_id_digest": manifest["host_machine_id_digest"],
            "installed_at": 1,
            "installer_binary_sha256": self.build["installer_binary_sha256"],
            "installer_source_manifest_digest": self.build[
                "source_manifest_digest"
            ],
            "package_authority_key_id": manifest["package_authority_key_id"],
            "plan_digest": manifest["plan_digest"],
            "previous_state_restored": False,
            "prior_authority_restored": False,
            "production_ready": False,
            "production_release_performed": False,
            "reactivation_performed": False,
            "reactivation_succeeded": False,
            "receipt_authority_key_id": manifest["receipt_authority_key_id"],
            "recovery_performed": False,
            "recovery_succeeded": False,
            "release_generation": manifest["release_generation"],
            "rollback_performed": False,
            "rollback_succeeded": False,
            "runtime_sha": manifest["runtime_sha"],
            "schema": "propertyquarry.release-control.single-host-install-receipt.v2",
            "systemd_socket_active": True,
            "upgraded_existing_authority": False,
            "version": 2,
            "workflow_sha": manifest["workflow_sha"],
        }
        receipt = self._receipt("install-receipt.json", payload)
        result = self._run("install", receipt)
        self.assertEqual(result.returncode, 0, result.stderr)
        verification = json.loads(result.stdout)
        self.assertIs(verification["signature_verified"], True)
        signed_wrapper = json.loads(receipt.read_text(encoding="utf-8"))
        signature = signed_wrapper["signature"]
        signed_wrapper["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        invalid_signature = self.fixture._write(
            "invalid-signature-receipt.json",
            package.canonical_json(signed_wrapper),
            0o600,
        )
        result = self._run("install", invalid_signature)
        self.assertEqual(result.returncode, 50)
        payload["archive_digest"] = "sha256:" + "f" * 64
        tampered = self._receipt("tampered-install-receipt.json", payload)
        result = self._run("install", tampered)
        self.assertEqual(result.returncode, 50)

    def test_install_receipt_rejects_nested_canary_tamper_and_replay(self) -> None:
        manifest = self.verified.manifest
        canary, canary_digest = self._canary(100)
        inner = canary["payload"]
        assert isinstance(inner, dict)
        base: dict[str, object] = {
            "activation_canary_challenge_sha256": inner["challenge_sha256"],
            "activation_canary_receipt": canary,
            "activation_canary_receipt_digest": canary_digest,
            "activation_canary_unit_sha256": inner["unit_sha256"],
            "activation_canary_valid_until": 220,
            "activation_canary_verified": True,
            "activation_canary_verified_at": 100,
            "activation_performed": True,
            "activation_succeeded": True,
            "archive_digest": self.verified.archive_sha256,
            "authority_installed": True,
            "authority_profile": package.PROFILE,
            "backup_encryption_key_created": False,
            "backup_encryption_key_id": "sha256:" + "b" * 64,
            "candidate_authority_installed": True,
            "config_digest": manifest["config_digest"],
            "deactivation_performed": False,
            "deactivation_succeeded": False,
            "disposition": "already-installed",
            "envelope_sha": manifest["envelope_sha"],
            "host_machine_id_digest": manifest["host_machine_id_digest"],
            "installed_at": 150,
            "installer_binary_sha256": self.build["installer_binary_sha256"],
            "installer_source_manifest_digest": self.build[
                "source_manifest_digest"
            ],
            "package_authority_key_id": manifest["package_authority_key_id"],
            "plan_digest": manifest["plan_digest"],
            "previous_state_restored": False,
            "prior_authority_restored": False,
            "production_ready": False,
            "production_release_performed": False,
            "reactivation_performed": False,
            "reactivation_succeeded": False,
            "receipt_authority_key_id": manifest["receipt_authority_key_id"],
            "recovery_performed": False,
            "recovery_succeeded": False,
            "release_generation": manifest["release_generation"],
            "rollback_performed": False,
            "rollback_succeeded": False,
            "runtime_sha": manifest["runtime_sha"],
            "schema": "propertyquarry.release-control.single-host-install-receipt.v2",
            "systemd_socket_active": True,
            "upgraded_existing_authority": False,
            "version": 2,
            "workflow_sha": manifest["workflow_sha"],
        }
        archived = self._receipt("archived-install-receipt.json", dict(base))
        self.assertEqual(self._run("install", archived).returncode, 0)
        outer_rebound = dict(base)
        outer_rebound["activation_canary_challenge_sha256"] = "sha256:" + "d" * 64
        self.assertEqual(
            self._run(
                "install",
                self._receipt("rebound-canary-receipt.json", outer_rebound),
            ).returncode,
            50,
        )
        expired = dict(base)
        expired["installed_at"] = 221
        self.assertEqual(
            self._run(
                "install", self._receipt("expired-canary-receipt.json", expired)
            ).returncode,
            50,
        )
        tampered = json.loads(json.dumps(base))
        tampered["activation_canary_receipt"]["payload"]["workflow_sha"] = "0" * 40
        tampered["activation_canary_receipt_digest"] = package.sha256(
            package.canonical_json(tampered["activation_canary_receipt"])
        )
        self.assertEqual(
            self._run(
                "install", self._receipt("tampered-canary-receipt.json", tampered)
            ).returncode,
            50,
        )
        rebound = json.loads(json.dumps(base))
        rebound_inner = rebound["activation_canary_receipt"]["payload"]
        rebound_inner["workflow_sha"] = "0" * 40
        rebound_signature = self.fixture.receipt_private.sign(
            package.framed(
                CANARY_DOMAIN, package.canonical_json(rebound_inner)
            )
        )
        rebound["activation_canary_receipt"]["signature"] = (
            base64.urlsafe_b64encode(rebound_signature)
            .rstrip(b"=")
            .decode("ascii")
        )
        rebound["activation_canary_receipt_digest"] = package.sha256(
            package.canonical_json(rebound["activation_canary_receipt"])
        )
        self.assertEqual(
            self._run(
                "install",
                self._receipt("signed-rebound-canary-receipt.json", rebound),
            ).returncode,
            50,
        )

    def test_signed_runner_receipt_is_fully_bound(self) -> None:
        manifest = self.verified.manifest
        payload: dict[str, object] = {
            "archive_bytes": 225628509,
            "archive_sha256": "sha256:4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf",
            "authority_manifest_digest": package.sha256(
                self.verified.members["manifest.v2.json"]
            ),
            "authority_profile": package.PROFILE,
            "disposition": "installed",
            "installed_at": 1,
            "installed_path": "/usr/lib/propertyquarry-release-runner-v2/actions-runner-linux-x64-2.335.1.tar.gz",
            "package_authority_key_id": manifest["package_authority_key_id"],
            "production_ready": False,
            "receipt_authority_key_id": manifest["receipt_authority_key_id"],
            "runner_archive_installed": True,
            "runner_registered": False,
            "schema": "propertyquarry.release-control.single-host-runner-install-receipt.v2",
            "version": 2,
        }
        receipt = self._receipt("runner-receipt.json", payload)
        result = self._run("runner", receipt)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
