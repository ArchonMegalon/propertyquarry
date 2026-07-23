from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_runner_reservation_tests",
    MODULE_ROOT / "tools" / "prepare-runner-reservation.py",
)
assert SPEC is not None and SPEC.loader is not None
reservation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reservation
SPEC.loader.exec_module(reservation)


class RunnerReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-runner-reservation-test-"
        )
        self.base = Path(self.temporary.name)
        os.chmod(self.base, 0o700)
        self.parent = self.base / "authority"
        self.parent.mkdir(mode=0o700)
        self.root = self.parent / "single-host-v2-runner-reservation"
        self.terminal_root = (
            self.parent / "single-host-v2-runner-reservation-terminal"
        )
        self.checkout_root = self.parent / "single-host-v2-release-checkouts"
        self.source = {
            "source_checkout_identity_sha256": "sha256:" + "b" * 64,
            "source_checkout_path": os.fspath(self.checkout_root / ("a" * 40)),
            "source_tree_sha256": "sha256:" + "c" * 64,
            "workflow_sha": "a" * 40,
        }
        self.private = Ed25519PrivateKey.generate()
        _der, self.key_id = reservation.materialize._public_identity(
            self.private.public_key()
        )
        self.patches = [
            mock.patch.object(reservation, "RESERVATION_PARENT", self.parent),
            mock.patch.object(reservation, "RESERVATION_ROOT", self.root),
            mock.patch.object(
                reservation,
                "RESERVATION_LOCK",
                self.parent / ".single-host-v2-runner-reservation.lock",
            ),
            mock.patch.object(
                reservation,
                "RESERVATION_STAGE",
                self.parent / ".single-host-v2-runner-reservation.preparing.v2",
            ),
            mock.patch.object(
                reservation, "RESERVATION_TERMINAL_ROOT", self.terminal_root
            ),
            mock.patch.object(
                reservation, "SOURCE_CHECKOUT_ROOT", self.checkout_root
            ),
            mock.patch.object(
                reservation,
                "_load_receipt_authority",
                return_value=(self.private, self.key_id),
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    @property
    def active_path(self) -> Path:
        return self.root / reservation.RESERVATION_NAME

    def prepare(self, now: int = 1_900_000_000):
        return reservation.prepare(
            now=now,
            random_source=lambda size: bytes(range(size)),
            source_observer=lambda: dict(self.source),
            source_validator=lambda payload: dict(self.source),
        )

    def test_prepare_is_signed_durable_idempotent_and_label_derived(self) -> None:
        result = self.prepare()
        self.assertEqual(result["disposition"], "prepared")
        self.assertRegex(result["runner_label"], reservation.LABEL_PATTERN)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.active_path.stat().st_mode & 0o777, 0o600)
        raw = self.active_path.read_bytes()
        payload = reservation._validate_wire(
            raw,
            workflow_sha="a" * 40,
            receipt_public=self.private.public_key(),
            receipt_id=self.key_id,
        )
        self.assertEqual(result["dispatch_ticket_sha256"], reservation._digest(raw))
        self.assertEqual(
            payload["expires_at_epoch"] - payload["created_at_epoch"], 21600
        )
        self.assertEqual(payload["runner_label_nonce"], payload["runner_label"][10:])

        def random_must_not_run(_size: int) -> bytes:
            raise AssertionError("idempotent prepare requested fresh randomness")

        validated = []
        repeated = reservation.prepare(
            now=1_900_000_001,
            random_source=random_must_not_run,
            source_observer=lambda: (_ for _ in ()).throw(
                AssertionError("idempotent prepare rebuilt the checkout")
            ),
            source_validator=lambda payload: validated.append(payload) or self.source,
        )
        self.assertEqual(len(validated), 1)
        self.assertEqual(repeated["disposition"], "already-prepared")
        self.assertEqual(
            repeated["dispatch_ticket_sha256"], result["dispatch_ticket_sha256"]
        )
        self.assertEqual(self.active_path.read_bytes(), raw)
        with self.assertRaisesRegex(
            reservation.ReservationFailure,
            "runner-reservation-checkout-revalidation-invalid",
        ):
            reservation.prepare(
                now=1_900_000_002,
                source_validator=lambda _payload: reservation.fail(
                    "runner-reservation-checkout-revalidation-invalid"
                ),
            )

    def test_noncanonical_signature_encoding_and_payload_rebinding_are_rejected(
        self,
    ) -> None:
        self.prepare()
        wrapper = json.loads(self.active_path.read_bytes())
        wrapper["signature"] += "="
        raw = reservation.materialize.package.canonical_json(wrapper)
        with self.assertRaises(reservation.ReservationFailure):
            reservation._validate_wire(
                raw,
                workflow_sha="a" * 40,
                receipt_public=self.private.public_key(),
                receipt_id=self.key_id,
            )

        wrapper = json.loads(self.active_path.read_bytes())
        payload = wrapper["payload"]
        payload["runner_label"] = "pqrelease-" + "f" * 32
        payload["runner_label_nonce"] = "f" * 32
        rebound = reservation._wire(payload, self.private, self.key_id)
        with self.assertRaisesRegex(
            reservation.ReservationFailure,
            "runner-reservation-label-binding-invalid",
        ):
            reservation._validate_wire(
                rebound,
                workflow_sha="a" * 40,
                receipt_public=self.private.public_key(),
                receipt_id=self.key_id,
            )

    def test_expired_terminal_publish_retry_and_duplicate_convergence(self) -> None:
        prepared = self.prepare(now=1000)
        current = 1000 + reservation.RESERVATION_TTL_SECONDS + 1
        first = reservation.recover_expired(now=current)
        self.assertEqual(first["disposition"], "expired-terminal-published")
        terminal = Path(first["terminal_path"]).parent
        self.assertFalse(self.root.exists())
        self.assertTrue(terminal.is_dir())
        self.assertEqual(
            terminal.name,
            prepared["dispatch_ticket_sha256"].removeprefix("sha256:")
            + ".expired.v2",
        )
        repeated = reservation.recover_expired(now=current)
        self.assertEqual(
            repeated["disposition"], "expired-terminal-already-published"
        )

        shutil.copytree(terminal, self.root)
        converged = reservation.recover_expired(now=current)
        self.assertEqual(converged["disposition"], "expired-duplicate-converged")
        self.assertFalse(self.root.exists())
        self.assertTrue(terminal.exists())

    def test_expired_recovery_authenticates_and_ignores_successful_bound_terminal(
        self,
    ) -> None:
        first = self.prepare(now=1000)
        raw = self.active_path.read_bytes()
        self.terminal_root.mkdir(mode=0o700)
        bound = self.terminal_root / (
            first["dispatch_ticket_sha256"].removeprefix("sha256:") + ".bound.v2"
        )
        binding_payload = {
            "authority_profile": "single-host-production-v2",
            "bound_at_epoch": 1001,
            "claim_sha256": "sha256:" + "1" * 64,
            "config_sha256": "sha256:" + "2" * 64,
            "config_signature_sha256": "sha256:" + "3" * 64,
            "deployment_id": "4" * 64,
            "environment": "propertyquarry-production",
            "job_id": "456",
            "materialization_receipt_sha256": "sha256:" + "5" * 64,
            "materialization_receipt_signature_sha256": "sha256:" + "6" * 64,
            "materialization_root": os.fspath(self.base / "materialization"),
            "materialization_root_identity_sha256": "sha256:" + "7" * 64,
            "plan_sha256": "sha256:" + "8" * 64,
            "receipt_authority_key_id": self.key_id,
            "reservation_sha256": first["dispatch_ticket_sha256"],
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": first["runner_label"],
            "runner_launch_ticket_sha256": "sha256:" + "9" * 64,
            "runner_prerequisite_approval_payload_sha256": "sha256:" + "a" * 64,
            "runner_prerequisite_approval_sha256": "sha256:" + "b" * 64,
            "runner_prerequisite_intent_sha256": "sha256:" + "c" * 64,
            "runner_prerequisite_job_id": "455",
            "runtime_sha": "b" * 40,
            "schema": reservation.materialize.RUNNER_MATERIALIZATION_BINDING_SCHEMA,
            "version": 2,
            "workflow_sha": "a" * 40,
        }
        bound.write_bytes(
            reservation.materialize._signed_runner_record(
                binding_payload,
                private=self.private,
                key_id=self.key_id,
                domain=reservation.materialize.RUNNER_MATERIALIZATION_BINDING_SIGNATURE_DOMAIN,
            )
        )
        os.chmod(bound, 0o600)
        shutil.rmtree(self.root)

        second = self.prepare(now=2000)
        current = 2000 + reservation.RESERVATION_TTL_SECONDS + 1
        recovered = reservation.recover_expired(now=current)
        self.assertEqual(recovered["disposition"], "expired-terminal-published")
        self.assertTrue(bound.exists())
        self.assertEqual(
            Path(recovered["terminal_path"]).parent.name,
            second["dispatch_ticket_sha256"].removeprefix("sha256:")
            + ".expired.v2",
        )

        bound.write_bytes(raw + b" ")
        with self.assertRaises(reservation.ReservationFailure):
            reservation.recover_expired(now=current)

    def test_staging_recovery_and_expiry_boundaries_fail_closed(self) -> None:
        reservation.RESERVATION_STAGE.mkdir(mode=0o700)
        staged = reservation.RESERVATION_STAGE / "partial"
        staged.write_bytes(b"partial")
        os.chmod(staged, 0o600)
        result = self.prepare(now=5000)
        self.assertEqual(result["disposition"], "prepared")
        self.assertFalse(reservation.RESERVATION_STAGE.exists())
        with self.assertRaisesRegex(
            reservation.ReservationFailure,
            "runner-reservation-not-expired",
        ):
            reservation.recover_expired(
                now=5000 + reservation.RESERVATION_TTL_SECONDS
            )
        with self.assertRaisesRegex(
            reservation.ReservationFailure,
            "runner-reservation-expired-recovery-required",
        ):
            reservation.prepare(
                now=5000 + reservation.RESERVATION_TTL_SECONDS + 1,
                source_observer=lambda: dict(self.source),
                source_validator=lambda payload: dict(self.source),
            )

    def test_cli_surface_has_no_caller_supplied_sha_or_label(self) -> None:
        parser = reservation.parser()
        parsed = parser.parse_args(["prepare"])
        self.assertEqual(vars(parsed), {"command": "prepare"})
        with self.assertRaises(SystemExit):
            parser.parse_args(["prepare", "a" * 40])
        with self.assertRaises(SystemExit):
            parser.parse_args(["prepare", "pqrelease-" + "a" * 32])

    def test_clean_detached_checkout_identity_tree_dirty_and_symlink_gates(
        self,
    ) -> None:
        self.checkout_root.mkdir(mode=0o700)
        build = self.base / "checkout-build"
        build.mkdir(mode=0o700)

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["/usr/bin/git", "-C", os.fspath(build), *arguments],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": "/nonexistent",
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
            )
            return completed.stdout.strip()

        git("init", "--quiet")
        os.chmod(build / ".git", 0o700)
        git("config", "user.name", "PropertyQuarry test")
        git("config", "user.email", "propertyquarry@example.invalid")
        git(
            "remote",
            "add",
            "origin",
            "https://github.com/ArchonMegalon/propertyquarry.git",
        )
        workflow = build / ".github/workflows/smoke-runtime.yml"
        workflow.parent.mkdir(mode=0o700, parents=True)
        workflow.write_text("name: fixture\n", encoding="utf-8")
        git("add", ".github/workflows/smoke-runtime.yml")
        git("commit", "--quiet", "-m", "fixture")
        workflow_sha = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", workflow_sha)
        git("checkout", "--quiet", "--detach", workflow_sha)
        target = self.checkout_root / workflow_sha
        build.rename(target)
        build = target
        os.chmod(target, 0o700)
        os.chmod(target / ".git", 0o700)
        workflow = target / ".github/workflows/smoke-runtime.yml"

        observed = reservation._validate_checkout(target, refresh=False)
        self.assertEqual(observed["workflow_sha"], workflow_sha)
        self.assertEqual(observed["source_checkout_path"], os.fspath(target))
        self.assertRegex(
            observed["source_checkout_identity_sha256"], r"^sha256:[0-9a-f]{64}$"
        )
        self.assertRegex(observed["source_tree_sha256"], r"^sha256:[0-9a-f]{64}$")

        symlink = self.checkout_root / ("f" * 40)
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaises(reservation.ReservationFailure):
            reservation._validate_checkout(symlink, refresh=False)

        workflow.write_text("name: dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(
            reservation.ReservationFailure,
            "runner-reservation-checkout-binding-invalid",
        ):
            reservation._validate_checkout(target, refresh=False)
        source = (MODULE_ROOT / "tools/prepare-runner-reservation.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("materialize.REPOSITORY_ROOT", source)


if __name__ == "__main__":
    unittest.main()
