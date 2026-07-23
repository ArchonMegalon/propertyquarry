from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_runtime_compose_sync_tests",
    MODULE_ROOT / "tools" / "sync-runtime-compose.py",
)
assert SPEC is not None and SPEC.loader is not None
compose_sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compose_sync
SPEC.loader.exec_module(compose_sync)

MATERIALIZE_SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_runtime_compose_sync_materialize_tests",
    MODULE_ROOT / "tools" / "materialize.py",
)
assert MATERIALIZE_SPEC is not None and MATERIALIZE_SPEC.loader is not None
materialize = importlib.util.module_from_spec(MATERIALIZE_SPEC)
sys.modules[MATERIALIZE_SPEC.name] = materialize
MATERIALIZE_SPEC.loader.exec_module(materialize)


class InjectedCrash(RuntimeError):
    pass


class RuntimeComposeSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-runtime-compose-sync-test-"
        )
        self.base = Path(self.temporary.name)
        os.chmod(self.base, 0o700)
        self.authority = self.base / "authority"
        self.authority.mkdir(mode=0o700)
        self.sync_root = self.authority / "compose-sync"
        self.lock = self.authority / ".reservation.lock"
        self.receipt_root = self.authority / "receipt-authority"
        self.property_root = self.base / "property"
        self.property_root.mkdir(mode=0o755)
        os.chmod(self.property_root, 0o755)
        self.property_target = self.property_root / "docker-compose.property.yml"
        self.cloudflared_target = (
            self.property_root / "docker-compose.cloudflared.yml"
        )
        self.old_property = b"services:\n  api:\n    image: old-property\n"
        self.old_cloudflared = b"services:\n  cloudflared:\n    image: old-cloud\n"
        self.new_property = b"services:\n  api:\n    image: new-property\n"
        self.new_cloudflared = b"services:\n  cloudflared:\n    image: new-cloud\n"
        self.property_target.write_bytes(self.old_property)
        self.cloudflared_target.write_bytes(self.old_cloudflared)
        os.chmod(self.property_target, 0o664)
        os.chmod(self.cloudflared_target, 0o664)
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key()
        self.receipt_id = "sha256:" + "a" * 64
        self.reservation_raw = b'{"fixture":"signed-reservation"}'
        self.reservation_payload = {
            "created_at_epoch": 1_900_000_000,
            "expires_at_epoch": 1_900_021_600,
            "runner_label": "pqrelease-" + "b" * 32,
            "source_checkout_path": os.fspath(self.base / ("c" * 40)),
            "workflow_sha": "c" * 40,
        }
        self.reservation_binding = {
            "reservation_sha256": compose_sync._digest(self.reservation_raw),
            "runner_label": self.reservation_payload["runner_label"],
            "source_checkout_identity_sha256": "sha256:" + "d" * 64,
            "source_checkout_path": self.reservation_payload[
                "source_checkout_path"
            ],
            "source_tree_sha256": "sha256:" + "e" * 64,
        }
        self.source_blobs = {
            "docker-compose.property.yml": self.new_property,
            "docker-compose.cloudflared.yml": self.new_cloudflared,
        }
        self.fake_materialize = SimpleNamespace(
            _load_authority=lambda _root: (
                b"",
                None,
                None,
                self.private,
                self.public,
                "sha256:" + "f" * 64,
                self.receipt_id,
            ),
            _validate_runner_checkout=lambda _payload: None,
        )
        self.patches = [
            mock.patch.object(
                compose_sync, "AUTHORITY_PARENT", self.authority
            ),
            mock.patch.object(compose_sync, "RESERVATION_LOCK", self.lock),
            mock.patch.object(
                compose_sync,
                "RESERVATION_ROOT",
                self.authority / "active-reservation",
            ),
            mock.patch.object(
                compose_sync, "RECEIPT_AUTHORITY_ROOT", self.receipt_root
            ),
            mock.patch.object(
                compose_sync, "COMPOSE_SYNC_ROOT", self.sync_root
            ),
            mock.patch.object(
                compose_sync,
                "COMPOSE_FILES",
                (
                    ("docker-compose.property.yml", self.property_target),
                    (
                        "docker-compose.cloudflared.yml",
                        self.cloudflared_target,
                    ),
                ),
            ),
            mock.patch.object(
                compose_sync,
                "_load_active_context",
                return_value=(
                    self.reservation_raw,
                    self.reservation_payload,
                    self.reservation_binding,
                    self.private,
                    self.public,
                    self.receipt_id,
                ),
            ),
            mock.patch.object(
                compose_sync,
                "_source_blobs",
                return_value=self.source_blobs,
            ),
            mock.patch.object(
                compose_sync,
                "_materialize_module",
                return_value=self.fake_materialize,
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    @property
    def expected_property(self) -> str:
        return compose_sync._digest(self.old_property)

    @property
    def expected_cloudflared(self) -> str:
        return compose_sync._digest(self.old_cloudflared)

    def sync(self, **kwargs):
        return compose_sync.sync_runtime_compose(
            expected_property_sha256=self.expected_property,
            expected_cloudflared_sha256=self.expected_cloudflared,
            now=1_900_000_100,
            random_source=lambda size: bytes(range(size)),
            **kwargs,
        )

    def verify(self):
        return compose_sync.verify_for_materialization(
            reservation_raw=self.reservation_raw,
            reservation_payload=self.reservation_payload,
            reservation_binding=self.reservation_binding,
            receipt_public=self.public,
            receipt_id=self.receipt_id,
        )

    def test_sync_commits_exact_blobs_modes_backups_and_signed_receipts(
        self,
    ) -> None:
        result = self.sync()
        self.assertEqual(result["disposition"], "committed")
        self.assertEqual(self.property_target.read_bytes(), self.new_property)
        self.assertEqual(
            self.cloudflared_target.read_bytes(), self.new_cloudflared
        )
        self.assertEqual(stat.S_IMODE(self.property_target.stat().st_mode), 0o644)
        self.assertEqual(
            stat.S_IMODE(self.cloudflared_target.stat().st_mode), 0o644
        )
        attempts = list(self.sync_root.iterdir())
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(stat.S_IMODE(attempt.stat().st_mode), 0o700)
        for index, expected in enumerate(
            (self.old_property, self.old_cloudflared)
        ):
            backup = attempt / compose_sync.BACKUP_DIRECTORY_NAME / f"{index}.old"
            candidate = (
                attempt / compose_sync.CANDIDATE_DIRECTORY_NAME / f"{index}.new"
            )
            self.assertEqual(backup.read_bytes(), expected)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o400)
            self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o400)
        intent_raw, intent, terminal_raw, terminal = (
            compose_sync._attempt_records(
                attempt,
                receipt_public=self.public,
                receipt_id=self.receipt_id,
                allow_pending=False,
            )
        )
        self.assertEqual(intent["reservation_sha256"], result["reservation_sha256"])
        self.assertIsNotNone(terminal_raw)
        self.assertEqual(terminal["disposition"], "committed")
        self.assertFalse(terminal["recovered"])
        self.assertEqual(
            terminal["intent_sha256"], compose_sync._digest(intent_raw)
        )
        self.assertEqual(self.verify()["terminal_sha256"], result["terminal_sha256"])
        repeated = self.sync()
        self.assertEqual(repeated["disposition"], "already-committed")
        self.assertEqual(len(list(self.sync_root.iterdir())), 1)
        with self.assertRaisesRegex(
            compose_sync.ComposeSyncFailure,
            "runtime-compose-sync-old-sha-cas-mismatch",
        ):
            compose_sync.sync_runtime_compose(
                expected_property_sha256="sha256:" + "0" * 64,
                expected_cloudflared_sha256=self.expected_cloudflared,
                now=1_900_000_101,
            )

    def test_old_sha_cas_and_target_metadata_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            compose_sync.ComposeSyncFailure,
            "runtime-compose-sync-old-sha-cas-mismatch",
        ):
            compose_sync.sync_runtime_compose(
                expected_property_sha256="sha256:" + "0" * 64,
                expected_cloudflared_sha256=self.expected_cloudflared,
                now=1_900_000_100,
            )
        self.assertEqual(self.property_target.read_bytes(), self.old_property)
        self.assertEqual(
            self.cloudflared_target.read_bytes(), self.old_cloudflared
        )
        self.assertEqual(list(self.sync_root.iterdir()), [])
        self.property_target.unlink()
        self.property_target.symlink_to(self.cloudflared_target)
        with self.assertRaisesRegex(
            compose_sync.ComposeSyncFailure,
            "runtime-compose-sync-file-metadata-invalid",
        ):
            self.sync()

    def test_crash_after_first_exchange_is_completed_deterministically(
        self,
    ) -> None:
        def checkpoint(name: str) -> None:
            if name == "after-target-0":
                raise InjectedCrash

        with self.assertRaises(InjectedCrash):
            self.sync(checkpoint=checkpoint)
        self.assertEqual(self.property_target.read_bytes(), self.new_property)
        self.assertEqual(
            self.cloudflared_target.read_bytes(), self.old_cloudflared
        )
        with self.assertRaisesRegex(
            compose_sync.ComposeSyncFailure, "runtime-compose-sync-pending"
        ):
            self.verify()
        recovered = compose_sync.recover_runtime_compose(now=1_900_000_101)
        self.assertEqual(recovered["committed_transactions"], 1)
        self.assertEqual(recovered["rolled_back_transactions"], 0)
        self.assertEqual(
            self.cloudflared_target.read_bytes(), self.new_cloudflared
        )
        verified = self.verify()
        attempt = self.sync_root / verified["transaction_id"]
        _intent_raw, _intent, _terminal_raw, terminal = (
            compose_sync._attempt_records(
                attempt,
                receipt_public=self.public,
                receipt_id=self.receipt_id,
                allow_pending=False,
            )
        )
        self.assertTrue(terminal["recovered"])
        self.assertEqual(terminal["disposition"], "committed")

    def test_crash_after_intent_rolls_back_then_allows_signed_retry(self) -> None:
        def checkpoint(name: str) -> None:
            if name == "after-intent":
                raise InjectedCrash

        with self.assertRaises(InjectedCrash):
            self.sync(checkpoint=checkpoint)
        recovered = compose_sync.recover_runtime_compose(now=1_900_000_101)
        self.assertEqual(recovered["committed_transactions"], 0)
        self.assertEqual(recovered["rolled_back_transactions"], 1)
        self.assertEqual(self.property_target.read_bytes(), self.old_property)
        self.assertEqual(
            self.cloudflared_target.read_bytes(), self.old_cloudflared
        )
        with self.assertRaisesRegex(
            compose_sync.ComposeSyncFailure,
            "runtime-compose-sync-committed-missing",
        ):
            self.verify()
        committed = compose_sync.sync_runtime_compose(
            expected_property_sha256=self.expected_property,
            expected_cloudflared_sha256=self.expected_cloudflared,
            now=1_900_000_102,
            random_source=lambda size: bytes(reversed(range(size))),
        )
        self.assertEqual(committed["disposition"], "committed")
        self.assertEqual(len(list(self.sync_root.iterdir())), 2)
        self.assertEqual(
            self.verify()["transaction_id"], committed["transaction_id"]
        )

    def test_torn_target_and_terminal_stages_are_recovered(self) -> None:
        def after_intent(name: str) -> None:
            if name == "after-intent":
                raise InjectedCrash

        with self.assertRaises(InjectedCrash):
            self.sync(checkpoint=after_intent)
        first_attempt = next(self.sync_root.iterdir())
        _raw, first_intent, _terminal_raw, _terminal = (
            compose_sync._attempt_records(
                first_attempt,
                receipt_public=self.public,
                receipt_id=self.receipt_id,
                allow_pending=True,
            )
        )
        torn_target_stage = Path(first_intent["files"][0]["stage_path"])
        torn_target_stage.touch(mode=0o644)
        os.chmod(torn_target_stage, 0o644)
        recovered = compose_sync.recover_runtime_compose(now=1_900_000_101)
        self.assertEqual(recovered["rolled_back_transactions"], 1)
        self.assertFalse(torn_target_stage.exists())

        def after_second_target(name: str) -> None:
            if name == "after-target-1":
                raise InjectedCrash

        with self.assertRaises(InjectedCrash):
            compose_sync.sync_runtime_compose(
                expected_property_sha256=self.expected_property,
                expected_cloudflared_sha256=self.expected_cloudflared,
                now=1_900_000_102,
                random_source=lambda size: bytes(reversed(range(size))),
                checkpoint=after_second_target,
            )
        second_attempt = next(
            attempt
            for attempt in self.sync_root.iterdir()
            if attempt != first_attempt
        )
        torn_terminal_stage = (
            second_attempt / compose_sync.TERMINAL_STAGE_NAME
        )
        torn_terminal_stage.touch(mode=0o600)
        os.chmod(torn_terminal_stage, 0o600)
        recovered = compose_sync.recover_runtime_compose(now=1_900_000_103)
        self.assertEqual(recovered["committed_transactions"], 1)
        self.assertFalse(torn_terminal_stage.exists())
        self.assertEqual(self.verify()["transaction_id"], second_attempt.name)

    def test_materializer_gate_rejects_pending_and_live_drift(self) -> None:
        def checkpoint(name: str) -> None:
            if name == "after-intent":
                raise InjectedCrash

        with self.assertRaises(InjectedCrash):
            self.sync(checkpoint=checkpoint)
        with mock.patch.object(
            materialize,
            "_load_runtime_compose_sync_module",
            return_value=compose_sync,
        ):
            with self.assertRaisesRegex(
                materialize.MaterializeFailure,
                "runtime-compose-sync-pending",
            ):
                materialize._verify_runtime_compose_sync(
                    reservation_raw=self.reservation_raw,
                    reservation_payload=self.reservation_payload,
                    reservation_binding=self.reservation_binding,
                    receipt_public=self.public,
                    receipt_id=self.receipt_id,
                )
        compose_sync.recover_runtime_compose(now=1_900_000_101)
        compose_sync.sync_runtime_compose(
            expected_property_sha256=self.expected_property,
            expected_cloudflared_sha256=self.expected_cloudflared,
            now=1_900_000_102,
            random_source=lambda size: bytes(reversed(range(size))),
        )
        self.property_target.write_bytes(b"services: {}\n")
        os.chmod(self.property_target, 0o644)
        with self.assertRaisesRegex(
            compose_sync.ComposeSyncFailure,
            "runtime-compose-sync-target-diverged",
        ):
            self.verify()

    def test_checkout_reader_uses_exact_git_blobs_and_requires_mode_100644(
        self,
    ) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.patches = []
        checkout = self.base / "checkout"
        checkout.mkdir(mode=0o700)

        def git(*args: str) -> str:
            completed = subprocess.run(
                ["/usr/bin/git", "-C", os.fspath(checkout), *args],
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
        git("config", "user.name", "PropertyQuarry test")
        git("config", "user.email", "propertyquarry@example.invalid")
        for name, raw in self.source_blobs.items():
            (checkout / name).write_bytes(raw)
        git("add", *self.source_blobs)
        git("commit", "--quiet", "-m", "fixture")
        workflow_sha = git("rev-parse", "HEAD")
        git("checkout", "--quiet", "--detach", workflow_sha)
        payload = {
            "source_checkout_path": os.fspath(checkout),
            "workflow_sha": workflow_sha,
        }
        self.assertEqual(compose_sync._source_blobs(payload), self.source_blobs)
        git("update-index", "--chmod=+x", "docker-compose.property.yml")
        git("commit", "--quiet", "-m", "make compose executable")
        executable_sha = git("rev-parse", "HEAD")
        payload["workflow_sha"] = executable_sha
        with self.assertRaisesRegex(
            compose_sync.ComposeSyncFailure,
            "runtime-compose-sync-source-mode-invalid",
        ):
            compose_sync._source_blobs(payload)

    def test_cli_requires_both_explicit_old_hashes(self) -> None:
        parser = compose_sync.parser()
        parsed = parser.parse_args(
            [
                "sync",
                "--expected-property-sha256",
                self.expected_property,
                "--expected-cloudflared-sha256",
                self.expected_cloudflared,
            ]
        )
        self.assertEqual(parsed.command, "sync")
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "sync",
                    "--expected-property-sha256",
                    self.expected_property,
                ]
            )


if __name__ == "__main__":
    unittest.main()
