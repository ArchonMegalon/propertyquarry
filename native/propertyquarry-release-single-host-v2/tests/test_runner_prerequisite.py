from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_runner_prerequisite_tests",
    MODULE_ROOT / "tools" / "approve-runner-prerequisite.py",
)
assert SPEC is not None and SPEC.loader is not None
approval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(approval)


class SimulatedCrash(RuntimeError):
    pass


class FakeGitHub:
    def __init__(self, reservation_payload: dict[str, object], *, ambiguous: bool = False):
        self.payload = reservation_payload
        self.approved = False
        self.posts = 0
        self.calls: list[tuple[str, str]] = []
        self.ambiguous = ambiguous

    @staticmethod
    def raw(value):
        return approval.reservation.materialize.package.canonical_json(value)

    def run(self, run_id: int) -> dict[str, object]:
        return {
            "conclusion": None,
            "created_at": "2030-03-17T17:46:45Z",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_repository": {"id": int(approval.REPOSITORY_ID)},
            "head_sha": self.payload["workflow_sha"],
            "id": run_id,
            "path": approval.WORKFLOW_PATH,
            "repository": {
                "full_name": approval.REPOSITORY,
                "id": int(approval.REPOSITORY_ID),
                "owner": {"id": int(approval.REPOSITORY_OWNER_ID)},
            },
            "run_attempt": 1,
            "status": "in_progress",
        }

    def jobs(self, run_id: int) -> dict[str, object]:
        prerequisite = {
            "conclusion": "success" if self.approved else None,
            "head_sha": self.payload["workflow_sha"],
            "id": run_id + 1000,
            "labels": ["ubuntu-latest"],
            "name": approval.PREREQUISITE_JOB,
            "run_url": (
                f"https://api.github.com/repos/{approval.REPOSITORY}/actions/runs/{run_id}"
            ),
            "status": "completed" if self.approved else "waiting",
        }
        return {"jobs": [prerequisite], "total_count": 1}

    @staticmethod
    def pending() -> list[dict[str, object]]:
        return [
            {
                "current_user_can_approve": True,
                "environment": {
                    "id": 42,
                    "name": approval.ENVIRONMENT,
                },
            }
        ]

    def __call__(
        self, method: str, path: str, body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append((method, path))
        prefix = f"/{approval.REPOSITORY_API}/actions"
        if path == (
            prefix
            + "/workflows/smoke-runtime.yml/runs?event=workflow_dispatch&branch=main&per_page=100"
        ):
            runs = [self.run(123)]
            if self.ambiguous:
                runs.append(self.run(124))
            return 200, self.raw({"total_count": len(runs), "workflow_runs": runs})
        for run_id in (123, 124):
            if path == f"{prefix}/runs/{run_id}/attempts/1/jobs?per_page=100":
                return 200, self.raw(self.jobs(run_id))
            if path == f"{prefix}/runs/{run_id}/pending_deployments":
                if method == "GET":
                    return 200, self.raw([] if self.approved else self.pending())
                self.posts += 1
                value = approval.reservation.materialize.package.parse_strict_json(
                    body or b"", "approval-test-body"
                )
                assert value == {
                    "comment": value["comment"],
                    "environment_ids": [42],
                    "state": "approved",
                }
                assert value["comment"].startswith(
                    "PropertyQuarry governed prerequisite approval sha256:"
                )
                self.approved = True
                return 200, self.raw([{"environment": approval.ENVIRONMENT, "id": 99}])
            if path == f"{prefix}/runs/{run_id}/approvals":
                if not self.approved:
                    return 200, self.raw([])
                comment = (
                    "PropertyQuarry governed prerequisite approval "
                    + approval._digest(self.reservation_raw)
                )
                return 200, self.raw(
                    [
                        {
                            "comment": comment,
                            "environments": [
                                {"id": 42, "name": approval.ENVIRONMENT}
                            ],
                            "state": "approved",
                            "user": {"id": 7, "login": "release-controller"},
                        }
                    ]
                )
        raise AssertionError(f"unexpected GitHub call: {method} {path}")


class RunnerPrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-runner-prerequisite-test-"
        )
        self.base = Path(self.temporary.name)
        os.chmod(self.base, 0o700)
        self.parent = self.base / "authority"
        self.parent.mkdir(mode=0o700)
        self.reservation_root = self.parent / "single-host-v2-runner-reservation"
        self.approval_root = (
            self.parent / "single-host-v2-runner-prerequisite-approvals"
        )
        self.checkout_root = self.parent / "single-host-v2-release-checkouts"
        self.private = Ed25519PrivateKey.generate()
        _der, self.key_id = approval.reservation.materialize._public_identity(
            self.private.public_key()
        )
        source = {
            "source_checkout_identity_sha256": "sha256:" + "b" * 64,
            "source_checkout_path": os.fspath(self.checkout_root / ("a" * 40)),
            "source_tree_sha256": "sha256:" + "c" * 64,
            "workflow_sha": "a" * 40,
        }
        self.patches = [
            mock.patch.object(
                approval.reservation, "RESERVATION_PARENT", self.parent
            ),
            mock.patch.object(
                approval.reservation, "RESERVATION_ROOT", self.reservation_root
            ),
            mock.patch.object(
                approval.reservation,
                "RESERVATION_LOCK",
                self.parent / ".single-host-v2-runner-reservation.lock",
            ),
            mock.patch.object(
                approval.reservation,
                "RESERVATION_STAGE",
                self.parent / ".single-host-v2-runner-reservation.preparing.v2",
            ),
            mock.patch.object(
                approval.reservation, "SOURCE_CHECKOUT_ROOT", self.checkout_root
            ),
            mock.patch.object(
                approval.reservation,
                "_load_receipt_authority",
                return_value=(self.private, self.key_id),
            ),
            mock.patch.object(approval, "APPROVAL_ROOT", self.approval_root),
        ]
        for patcher in self.patches:
            patcher.start()
        prepared = approval.reservation.prepare(
            now=1_900_000_000,
            random_source=lambda size: bytes(range(size)),
            source_observer=lambda: dict(source),
            source_validator=lambda payload: dict(source),
        )
        self.reservation_raw = (
            self.reservation_root / approval.reservation.RESERVATION_NAME
        ).read_bytes()
        self.reservation_payload = approval.reservation._validate_wire(
            self.reservation_raw,
            workflow_sha="a" * 40,
            receipt_public=self.private.public_key(),
            receipt_id=self.key_id,
        )
        self.assertEqual(
            prepared["dispatch_ticket_sha256"],
            approval._digest(self.reservation_raw),
        )

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def fake(self, *, ambiguous: bool = False) -> FakeGitHub:
        github = FakeGitHub(self.reservation_payload, ambiguous=ambiguous)
        github.reservation_raw = self.reservation_raw
        return github

    def approve(self, github: FakeGitHub):
        return approval.approve(
            now=1_900_000_005,
            requester=github,
            current_time=lambda: 1_900_000_010,
            sleeper=lambda _seconds: None,
        )

    def test_exact_prerequisite_transition_is_signed_and_idempotent(self) -> None:
        github = self.fake()
        result = self.approve(github)
        self.assertEqual(result["disposition"], "approved")
        self.assertEqual(result["run_id"], "123")
        self.assertEqual(result["run_attempt"], 1)
        self.assertEqual(result["prerequisite_job_id"], "1123")
        self.assertEqual(github.posts, 1)

        intent_path, approved_path = approval._record_paths(self.reservation_raw)
        self.assertEqual(intent_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(approved_path.stat().st_mode & 0o777, 0o600)
        intent_raw = intent_path.read_bytes()
        intent = approval._verify_wire(
            intent_raw,
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.INTENT_SCHEMA,
            domain=approval.INTENT_SIGNATURE_DOMAIN,
        )
        approval._validate_intent(
            intent,
            self.reservation_raw,
            self.reservation_payload,
            self.key_id,
        )
        approved_raw = approved_path.read_bytes()
        approved = approval._verify_wire(
            approved_raw,
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.APPROVAL_SCHEMA,
            domain=approval.APPROVAL_SIGNATURE_DOMAIN,
        )
        approval._validate_approval(
            approved, intent_raw=intent_raw, intent=intent
        )
        self.assertEqual(approved["prerequisite_conclusion"], "success")

        repeated = approval.approve(
            now=1_900_000_011,
            requester=lambda *_args: (_ for _ in ()).throw(
                AssertionError("idempotent approval reached GitHub")
            ),
        )
        self.assertEqual(repeated["disposition"], "already-approved")
        self.assertEqual(repeated["approval_sha256"], result["approval_sha256"])

    def test_post_approval_crash_recovers_from_signed_intent_and_review(self) -> None:
        github = self.fake()

        def checkpoint(name: str) -> None:
            if name == "after-runner-prerequisite-approval-post":
                raise SimulatedCrash(name)

        with mock.patch.object(approval, "_checkpoint", side_effect=checkpoint):
            with self.assertRaises(SimulatedCrash):
                self.approve(github)
        intent_path, approved_path = approval._record_paths(self.reservation_raw)
        self.assertTrue(intent_path.exists())
        self.assertFalse(approved_path.exists())
        self.assertEqual(github.posts, 1)

        recovered = self.approve(github)
        self.assertEqual(recovered["disposition"], "post-approved-recovered")
        self.assertEqual(github.posts, 1)
        approved = approval._verify_wire(
            approved_path.read_bytes(),
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.APPROVAL_SCHEMA,
            domain=approval.APPROVAL_SIGNATURE_DOMAIN,
        )
        self.assertEqual(approved["approval_api_disposition"], "post-approved-recovered")
        self.assertIsNone(approved["approval_response_sha256"])

    def test_ambiguous_stale_and_release_job_states_never_approve(self) -> None:
        ambiguous = self.fake(ambiguous=True)
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-dispatch-selection-invalid",
        ):
            self.approve(ambiguous)
        self.assertEqual(ambiguous.posts, 0)

        stale = self.fake()
        stale.payload = dict(stale.payload)
        stale.payload["workflow_sha"] = "f" * 40
        with self.assertRaises(approval.ApprovalFailure):
            self.approve(stale)
        self.assertEqual(stale.posts, 0)

        release_present = self.fake()
        original_jobs = release_present.jobs

        def jobs_with_release(run_id: int):
            value = original_jobs(run_id)
            value["jobs"].append(
                {"id": 9999, "name": approval.RELEASE_JOB, "status": "queued"}
            )
            value["total_count"] = 2
            return value

        release_present.jobs = jobs_with_release  # type: ignore[method-assign]
        with self.assertRaises(approval.ApprovalFailure):
            self.approve(release_present)
        self.assertEqual(release_present.posts, 0)

    def test_staging_residue_is_recovered_but_unknown_entries_fail_closed(self) -> None:
        self.approval_root.mkdir(mode=0o700)
        stage = self.approval_root / (
            ".runner-prerequisite-" + "f" * 64 + ".tmp"
        )
        stage.write_bytes(b"interrupted")
        os.chmod(stage, 0o600)
        github = self.fake()
        result = self.approve(github)
        self.assertEqual(result["disposition"], "approved")
        self.assertFalse(stage.exists())

        unknown = self.approval_root / "unexpected"
        unknown.write_bytes(b"tamper")
        os.chmod(unknown, 0o600)
        with self.assertRaisesRegex(
            approval.ApprovalFailure, "runner-prerequisite-root-entry-invalid"
        ):
            approval.approve(
                now=1_900_000_011,
                requester=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("tampered root reached GitHub")
                ),
            )

    def test_workflow_has_exact_pre_release_protected_gate(self) -> None:
        workflow = (
            MODULE_ROOT.parents[1] / ".github/workflows/smoke-runtime.yml"
        ).read_text(encoding="utf-8")
        start = workflow.index("  propertyquarry-protected-dispatch-inputs:\n")
        end = workflow.index("\n  propertyquarry-mirror-role-contract:", start)
        prerequisite = workflow[start:end]
        self.assertIn("    environment:\n      name: propertyquarry-production\n", prerequisite)
        self.assertIn("    permissions:\n      contents: none\n", prerequisite)
        self.assertIn("    runs-on: ubuntu-latest\n", prerequisite)
        self.assertNotIn("propertyquarry-release-controller-v2", prerequisite)

        release_start = workflow.index("  propertyquarry-release-v2:\n")
        release_end = workflow.index("\n  propertyquarry-activation-request-inert:", release_start)
        release = workflow[release_start:release_end]
        self.assertIn("      - propertyquarry-protected-dispatch-inputs\n", release)
        self.assertIn("      name: propertyquarry-production\n", release)
        self.assertIn("      - propertyquarry-release-controller-v2\n", release)


if __name__ == "__main__":
    unittest.main()
