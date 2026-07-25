from __future__ import annotations

import importlib.util
import json
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
    def __init__(
        self,
        reservation_payload: dict[str, object],
        *,
        ambiguous: bool = False,
        history_delay: int = 0,
        other_sha_run_count: int = 0,
        pending_delay: int = 0,
        same_sha_total_count_extra: int = 0,
    ):
        self.payload = reservation_payload
        self.query_workflow_sha = reservation_payload["workflow_sha"]
        self.approved = False
        self.terminal = False
        self.run_conclusion = "cancelled"
        self.prerequisite_conclusion = "cancelled"
        self.posts = 0
        self.calls: list[tuple[str, str]] = []
        self.ambiguous = ambiguous
        self.other_sha_run_count = other_sha_run_count
        self.same_sha_total_count_extra = same_sha_total_count_extra
        self.history_delay = history_delay
        self.pending_delay = pending_delay
        self.history_reads_after_approval = 0
        self.pending_reads_after_approval = 0
        self.duplicate_reviews = False
        self.release_job_mode = "absent"
        self.release_job_labels_override: list[str] | None = None
        self.manual_environment_approval = False
        self.current_run_attempt = 1
        self.job_name_override: str | None = None

    @staticmethod
    def raw(value):
        return approval.reservation.materialize.package.canonical_json(value)

    def prerequisite_name(self) -> str:
        if self.job_name_override is not None:
            return self.job_name_override
        return approval._prerequisite_job_name(
            runner_label=str(self.payload["runner_label"]),
            reservation_sha256=approval._digest(self.reservation_raw),
        )

    def run(self, run_id: int) -> dict[str, object]:
        return {
            "conclusion": self.run_conclusion if self.terminal else None,
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
            "run_attempt": self.current_run_attempt,
            "status": "completed" if self.terminal else "in_progress",
        }

    def jobs(self, run_id: int) -> dict[str, object]:
        prerequisite = {
            "conclusion": (
                self.prerequisite_conclusion
                if self.terminal
                else ("success" if self.approved else None)
            ),
            "head_sha": self.payload["workflow_sha"],
            "id": run_id + 1000,
            "labels": ["ubuntu-latest"],
            "name": self.prerequisite_name(),
            "run_url": (
                f"https://api.github.com/repos/{approval.REPOSITORY}/actions/runs/{run_id}"
            ),
            "status": "completed" if self.approved or self.terminal else "waiting",
        }
        jobs = [prerequisite]
        if self.release_job_mode == "queued":
            jobs.append(
                {
                    "conclusion": None,
                    "head_sha": self.payload["workflow_sha"],
                    "id": run_id + 2000,
                    "labels": ["propertyquarry-release-controller-v2"],
                    "name": approval.RELEASE_JOB,
                    "run_url": (
                        f"https://api.github.com/repos/{approval.REPOSITORY}/"
                        f"actions/runs/{run_id}"
                    ),
                    "status": "queued",
                }
            )
        elif self.release_job_mode in {
            "inert",
            "inert-stamped",
            "inert-started",
            "executed",
        }:
            executed = self.release_job_mode == "executed"
            if self.release_job_mode == "inert-stamped":
                started_at = "2030-03-17T18:00:00Z"
            elif self.release_job_mode == "inert-started":
                started_at = "2030-03-17T17:59:00Z"
            else:
                started_at = (
                    "2030-03-17T17:59:00Z" if executed else None
                )
            jobs.append(
                {
                    "completed_at": "2030-03-17T18:00:00Z",
                    "conclusion": "cancelled",
                    "head_sha": self.payload["workflow_sha"],
                    "id": run_id + 2000,
                    "labels": (
                        self.release_job_labels_override
                        if self.release_job_labels_override is not None
                        else [
                            "propertyquarry-release-controller-v2",
                            self.payload["runner_label"],
                        ]
                    ),
                    "name": approval.RELEASE_JOB,
                    "run_attempt": 1,
                    "run_url": (
                        f"https://api.github.com/repos/{approval.REPOSITORY}/"
                        f"actions/runs/{run_id}"
                    ),
                    "runner_group_id": None,
                    "runner_group_name": None,
                    "runner_id": 77 if executed else None,
                    "runner_name": "release-host" if executed else None,
                    "started_at": started_at,
                    "status": "completed",
                    "steps": [{"name": "ran"}] if executed else [],
                }
            )
        return {"jobs": jobs, "total_count": len(jobs)}

    @staticmethod
    def pending() -> list[dict[str, object]]:
        return [
            {
                "current_user_can_approve": True,
                "environment": {"id": 42, "name": approval.ENVIRONMENT},
            }
        ]

    def __call__(
        self, method: str, path: str, body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append((method, path))
        prefix = f"/{approval.REPOSITORY_API}/actions"
        filtered_runs_path = (
            prefix
            + "/workflows/smoke-runtime.yml/runs?event=workflow_dispatch"
            + "&branch=main"
            + f"&head_sha={self.query_workflow_sha}&per_page=100"
        )
        unfiltered_runs_path = (
            prefix
            + "/workflows/smoke-runtime.yml/runs?event=workflow_dispatch"
            + "&branch=main&per_page=100"
        )
        if path in {filtered_runs_path, unfiltered_runs_path}:
            runs = [self.run(123)]
            if self.ambiguous:
                runs.append(self.run(124))
            total_extra = self.same_sha_total_count_extra
            if path == unfiltered_runs_path:
                total_extra += self.other_sha_run_count
            return 200, self.raw(
                {
                    "total_count": len(runs) + total_extra,
                    "workflow_runs": runs,
                }
            )
        for run_id in (123, 124):
            if path == f"{prefix}/runs/{run_id}" and method == "GET":
                return 200, self.raw(self.run(run_id))
            if path == f"{prefix}/runs/{run_id}/attempts/1/jobs?per_page=100":
                return 200, self.raw(self.jobs(run_id))
            if path == f"{prefix}/runs/{run_id}/pending_deployments":
                if method == "GET":
                    if self.terminal:
                        return 200, self.raw([])
                    if self.approved:
                        self.pending_reads_after_approval += 1
                        if self.pending_reads_after_approval > self.pending_delay:
                            return 200, self.raw([])
                    return 200, self.raw(self.pending())
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
                return 200, self.raw(
                    [{"environment": approval.ENVIRONMENT, "id": 99}]
                )
            if path == f"{prefix}/runs/{run_id}/approvals":
                if not self.approved and not self.manual_environment_approval:
                    return 200, self.raw([])
                self.history_reads_after_approval += 1
                if self.history_reads_after_approval <= self.history_delay:
                    return 200, self.raw([])
                comment = (
                    "PropertyQuarry governed prerequisite approval "
                    + approval._digest(self.reservation_raw)
                )
                if self.manual_environment_approval and not self.approved:
                    comment = "manual emergency approval"
                review = {
                    "comment": comment,
                    "environments": [
                        {"id": 42, "name": approval.ENVIRONMENT}
                    ],
                    "state": "approved",
                    "user": {"id": 7, "login": "release-controller"},
                }
                return 200, self.raw(
                    [review, dict(review)]
                    if self.duplicate_reviews
                    else [review]
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
        self.terminal_root = (
            self.parent / "single-host-v2-runner-reservation-terminal"
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
                "RESERVATION_TERMINAL_ROOT",
                self.terminal_root,
            ),
            mock.patch.object(
                approval.reservation,
                "PREREQUISITE_APPROVAL_ROOT",
                self.approval_root,
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

    def fake(self, **options) -> FakeGitHub:
        github = FakeGitHub(self.reservation_payload, **options)
        github.reservation_raw = self.reservation_raw
        return github

    def approve(self, github: FakeGitHub):
        return approval.approve(
            now=1_900_000_005,
            requester=github,
            current_time=lambda: 1_900_000_010,
            sleeper=lambda _seconds: None,
        )

    def retire(self, github: FakeGitHub, *, now: int = 1_900_000_020):
        return approval.retire_terminal(
            now=now,
            requester=github,
            current_time=lambda: now,
        )

    def discover_intent(self, github: FakeGitHub) -> None:
        def checkpoint(name: str) -> None:
            if name == "after-runner-prerequisite-intent":
                raise SimulatedCrash(name)

        with mock.patch.object(approval, "_checkpoint", side_effect=checkpoint):
            with self.assertRaises(SimulatedCrash):
                self.approve(github)

    def convert_intent_to_frozen_v2(self) -> Path:
        intent_path, _approval_path = approval._record_paths(
            self.reservation_raw
        )
        payload = approval._verify_wire(
            intent_path.read_bytes(),
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.INTENT_SCHEMA,
            domain=approval.INTENT_SIGNATURE_DOMAIN,
        )
        frozen = dict(payload)
        frozen.pop("prerequisite_job_key")
        frozen["prerequisite_job_name"] = approval.PREREQUISITE_JOB_KEY
        frozen["schema"] = approval.LEGACY_INTENT_SCHEMA
        frozen["version"] = 2
        frozen_raw = approval._wire(
            frozen,
            self.private,
            self.key_id,
            approval.LEGACY_INTENT_SIGNATURE_DOMAIN,
        )
        legacy_path, _legacy_approval_path = approval._legacy_record_paths(
            self.reservation_raw
        )
        legacy_path.write_bytes(frozen_raw)
        os.chmod(legacy_path, 0o600)
        intent_path.unlink()
        return legacy_path

    def write_materialization_claim(self, *, pending: bool = False) -> Path:
        self.terminal_root.mkdir(mode=0o700, exist_ok=True)
        payload = {
            "authority_profile": "single-host-production-v2",
            "claimed_at_epoch": 1_900_000_007,
            "deployment_id": "d" * 64,
            "environment": approval.reservation.materialize.package.ENVIRONMENT,
            "expires_at_epoch": 1_900_000_107,
            "materialization_parent_identity_sha256": "sha256:" + "1" * 64,
            "materialization_root": os.fspath(self.base / "materialization"),
            "receipt_authority_key_id": self.key_id,
            "release_evidence_sha256": "sha256:" + "2" * 64,
            "reservation_nonce": self.reservation_payload["reservation_nonce"],
            "reservation_sha256": approval._digest(self.reservation_raw),
            "runner_prerequisite_approval_payload_sha256": (
                "sha256:" + "3" * 64
            ),
            "runner_prerequisite_approval_sha256": "sha256:" + "4" * 64,
            "runner_prerequisite_intent_sha256": "sha256:" + "5" * 64,
            "runner_prerequisite_job_id": "1123",
            "runner_label": self.reservation_payload["runner_label"],
            "runtime_sha": "b" * 40,
            "schema": (
                approval.reservation.materialize.
                RUNNER_MATERIALIZATION_CLAIM_SCHEMA
            ),
            "version": 2,
            "workflow_sha": self.reservation_payload["workflow_sha"],
        }
        raw = approval.reservation.materialize._signed_runner_record(
            payload,
            private=self.private,
            key_id=self.key_id,
            domain=(
                approval.reservation.materialize.
                RUNNER_MATERIALIZATION_CLAIM_SIGNATURE_DOMAIN
            ),
        )
        suffix = ".claim.v2.pending" if pending else ".claim.v2"
        path = self.terminal_root / (
            approval._digest(self.reservation_raw).removeprefix("sha256:")
            + suffix
        )
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        return path

    def write_materialization_binding(self, *, pending: bool = False) -> Path:
        self.terminal_root.mkdir(mode=0o700, exist_ok=True)
        payload = {
            "authority_profile": "single-host-production-v2",
            "bound_at_epoch": 1_900_000_008,
            "claim_sha256": "sha256:" + "1" * 64,
            "config_sha256": "sha256:" + "2" * 64,
            "config_signature_sha256": "sha256:" + "3" * 64,
            "deployment_id": "d" * 64,
            "environment": approval.reservation.materialize.package.ENVIRONMENT,
            "job_id": "2123",
            "materialization_receipt_sha256": "sha256:" + "4" * 64,
            "materialization_receipt_signature_sha256": (
                "sha256:" + "5" * 64
            ),
            "materialization_root": os.fspath(self.base / "materialization"),
            "materialization_root_identity_sha256": "sha256:" + "6" * 64,
            "plan_sha256": "sha256:" + "7" * 64,
            "receipt_authority_key_id": self.key_id,
            "reservation_sha256": approval._digest(self.reservation_raw),
            "run_attempt": 1,
            "run_id": "123",
            "runner_label": self.reservation_payload["runner_label"],
            "runner_launch_ticket_sha256": "sha256:" + "8" * 64,
            "runner_prerequisite_approval_payload_sha256": (
                "sha256:" + "9" * 64
            ),
            "runner_prerequisite_approval_sha256": "sha256:" + "a" * 64,
            "runner_prerequisite_intent_sha256": "sha256:" + "b" * 64,
            "runner_prerequisite_job_id": "1123",
            "runtime_sha": "b" * 40,
            "schema": (
                approval.reservation.materialize.
                RUNNER_MATERIALIZATION_BINDING_SCHEMA
            ),
            "version": 2,
            "workflow_sha": self.reservation_payload["workflow_sha"],
        }
        raw = approval.reservation.materialize._signed_runner_record(
            payload,
            private=self.private,
            key_id=self.key_id,
            domain=(
                approval.reservation.materialize.
                RUNNER_MATERIALIZATION_BINDING_SIGNATURE_DOMAIN
            ),
        )
        suffix = ".bound.v2.pending" if pending else ".bound.v2"
        path = self.terminal_root / (
            approval._digest(self.reservation_raw).removeprefix("sha256:")
            + suffix
        )
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        return path

    def publish_expired_terminal(self) -> Path:
        self.terminal_root.mkdir(mode=0o700, exist_ok=True)
        terminal = self.terminal_root / (
            approval._digest(self.reservation_raw).removeprefix("sha256:")
            + ".expired.v2"
        )
        self.reservation_root.rename(terminal)
        return terminal

    def restore_active_duplicate(self) -> None:
        self.reservation_root.mkdir(mode=0o700)
        ticket = (
            self.reservation_root
            / approval.reservation.RESERVATION_NAME
        )
        ticket.write_bytes(self.reservation_raw)
        os.chmod(ticket, 0o600)

    def removal_tombstone(self) -> Path:
        return self.terminal_root / (
            approval._digest(self.reservation_raw).removeprefix("sha256:")
            + ".removing.v2"
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
        attempt_path = approval._post_attempt_path(self.reservation_raw)
        attempt_raw = attempt_path.read_bytes()
        attempt = approval._verify_wire(
            attempt_raw,
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.POST_ATTEMPT_SCHEMA,
            domain=approval.POST_ATTEMPT_SIGNATURE_DOMAIN,
        )
        approval._validate_post_attempt(
            attempt, intent_raw=intent_raw, intent=intent
        )
        self.assertEqual(
            approved["approved_at_epoch"], attempt["attempted_at_epoch"]
        )

        repeated = approval.approve(
            now=1_900_000_011,
            requester=lambda *_args: (_ for _ in ()).throw(
                AssertionError("idempotent approval reached GitHub")
            ),
        )
        self.assertEqual(repeated["disposition"], "already-approved")
        self.assertEqual(repeated["approval_sha256"], result["approval_sha256"])
        with mock.patch.object(
            approval,
            "_read_admin_token",
            side_effect=AssertionError(
                "idempotent approval consumed the token descriptor"
            ),
        ):
            token_free_repeat = approval.approve(now=1_900_000_011)
        self.assertEqual(
            token_free_repeat["disposition"], "already-approved"
        )
        mismatched = dict(approved)
        mismatched["approved_at_epoch"] += 1
        approved_path.write_bytes(
            approval._wire(
                mismatched,
                self.private,
                self.key_id,
                approval.APPROVAL_SIGNATURE_DOMAIN,
            )
        )
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-governed-state-invalid",
        ):
            approval.approve(
                now=1_900_000_011,
                requester=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("mismatched approval reached GitHub")
                ),
            )
        approved_path.write_bytes(approved_raw)
        attempt_path.unlink()
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-governed-state-invalid",
        ):
            approval.approve(
                now=1_900_000_011,
                requester=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("unarmed approval reached GitHub")
                ),
            )

    def test_partial_token_read_is_wiped_on_exception(self) -> None:
        observed: list[bytearray] = []

        class TrackingBytearray(bytearray):
            def __init__(self, *arguments):
                super().__init__(*arguments)
                observed.append(self)

        metadata = type(
            "TokenStat",
            (),
            {"st_mode": approval.stat.S_IFIFO | 0o600},
        )()
        with (
            mock.patch.dict(
                approval.os.environ,
                {"PROPERTYQUARRY_RUNNER_PREREQUISITE_TOKEN_FD": "8"},
            ),
            mock.patch.object(approval.os, "fstat", return_value=metadata),
            mock.patch.object(
                approval.os,
                "read",
                side_effect=[
                    b"github_pat_partially_read_secret",
                    OSError("token source failed"),
                ],
            ),
            mock.patch.object(
                approval,
                "bytearray",
                TrackingBytearray,
                create=True,
            ),
        ):
            with self.assertRaisesRegex(OSError, "token source failed"):
                approval._read_admin_token()
        self.assertEqual(len(observed), 1)
        self.assertGreater(len(observed[0]), 0)
        self.assertEqual(observed[0], b"\0" * len(observed[0]))

    def test_signed_wrappers_must_be_canonical_bytes(self) -> None:
        github = self.fake()
        self.discover_intent(github)
        intent_path, _approval_path = approval._record_paths(
            self.reservation_raw
        )
        wrapper = json.loads(intent_path.read_bytes())
        noncanonical = json.dumps(wrapper, indent=2).encode("utf-8")
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-wire-invalid",
        ):
            approval._verify_wire(
                noncanonical,
                public=self.private.public_key(),
                key_id=self.key_id,
                schema=approval.INTENT_SCHEMA,
                domain=approval.INTENT_SIGNATURE_DOMAIN,
            )

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

    def test_post_attempt_reconciles_after_reservation_expiry(self) -> None:
        github = self.fake()
        expires = self.reservation_payload["expires_at_epoch"]
        self.assertIsInstance(expires, int)

        def checkpoint(name: str) -> None:
            if name == "after-runner-prerequisite-approval-post":
                raise SimulatedCrash(name)

        with mock.patch.object(approval, "_checkpoint", side_effect=checkpoint):
            with self.assertRaises(SimulatedCrash):
                approval.approve(
                    now=expires - 1,
                    requester=github,
                    current_time=lambda: expires - 1,
                    sleeper=lambda _seconds: None,
                )
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-prerequisite-post-attempt-present",
        ):
            approval.reservation.recover_expired(now=expires + 1)
        self.assertTrue(self.reservation_root.exists())
        recovered = approval.approve(
            now=expires + 10,
            requester=github,
            current_time=lambda: expires + 10,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(
            recovered["disposition"], "post-approved-recovered"
        )
        self.assertEqual(github.posts, 1)

    def test_delayed_history_and_stale_pending_reconcile_without_repost(
        self,
    ) -> None:
        github = self.fake(history_delay=2, pending_delay=3)
        result = self.approve(github)
        self.assertEqual(result["disposition"], "approved")
        self.assertEqual(github.posts, 1)
        self.assertGreaterEqual(github.history_reads_after_approval, 3)
        self.assertGreaterEqual(github.pending_reads_after_approval, 4)

    def test_approval_reconcile_rejects_run_attempt_drift(self) -> None:
        github = self.fake()

        def checkpoint(name: str) -> None:
            if name == "after-runner-prerequisite-approval-post":
                github.current_run_attempt = 2

        with mock.patch.object(approval, "_checkpoint", side_effect=checkpoint):
            with self.assertRaisesRegex(
                approval.ApprovalFailure,
                "runner-prerequisite-run-binding-invalid",
            ):
                self.approve(github)
        self.assertEqual(github.posts, 1)

    def test_crash_boundaries_before_post_are_at_most_once(self) -> None:
        after_intent = self.fake()
        self.discover_intent(after_intent)
        self.assertEqual(after_intent.posts, 0)
        recovered = self.approve(after_intent)
        self.assertEqual(recovered["disposition"], "approved")
        self.assertEqual(after_intent.posts, 1)

        self.tearDown()
        self.setUp()
        after_attempt = self.fake()

        def checkpoint(name: str) -> None:
            if name == "after-runner-prerequisite-post-attempt":
                raise SimulatedCrash(name)

        with mock.patch.object(approval, "_checkpoint", side_effect=checkpoint):
            with self.assertRaises(SimulatedCrash):
                self.approve(after_attempt)
        self.assertEqual(after_attempt.posts, 0)
        with mock.patch.object(approval, "MAXIMUM_RECONCILIATION_POLLS", 3):
            with self.assertRaisesRegex(
                approval.ApprovalFailure,
                "runner-prerequisite-completion-timeout",
            ):
                self.approve(after_attempt)
        self.assertEqual(after_attempt.posts, 0)

    def test_unarmed_exact_review_and_duplicates_fail_closed(self) -> None:
        unarmed = self.fake(pending_delay=2)
        self.discover_intent(unarmed)
        unarmed.approved = True
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-unarmed-review-present",
        ):
            self.approve(unarmed)
        self.assertEqual(unarmed.posts, 0)
        self.assertFalse(
            approval._post_attempt_path(self.reservation_raw).exists()
        )

        self.tearDown()
        self.setUp()
        duplicated = self.fake()
        self.discover_intent(duplicated)
        duplicated.approved = True
        duplicated.duplicate_reviews = True
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-review-duplicated",
        ):
            self.approve(duplicated)
        self.assertEqual(duplicated.posts, 0)

    def test_pre_attempt_gate_rechecks_attempt_review_and_material(self) -> None:
        rerun = self.fake()

        def rerun_checkpoint(name: str) -> None:
            if name == "before-runner-prerequisite-post-attempt-gate":
                rerun.current_run_attempt = 2

        with mock.patch.object(
            approval, "_checkpoint", side_effect=rerun_checkpoint
        ):
            with self.assertRaisesRegex(
                approval.ApprovalFailure,
                "runner-prerequisite-run-binding-invalid",
            ):
                self.approve(rerun)
        self.assertEqual(rerun.posts, 0)
        self.assertFalse(
            approval._post_attempt_path(self.reservation_raw).exists()
        )

        self.tearDown()
        self.setUp()
        external_approval = self.fake()

        def approval_checkpoint(name: str) -> None:
            if name == "before-runner-prerequisite-post-attempt-gate":
                external_approval.manual_environment_approval = True

        with mock.patch.object(
            approval, "_checkpoint", side_effect=approval_checkpoint
        ):
            with self.assertRaisesRegex(
                approval.ApprovalFailure,
                "runner-prerequisite-review-conflict",
            ):
                self.approve(external_approval)
        self.assertEqual(external_approval.posts, 0)

        self.tearDown()
        self.setUp()
        exact_approval = self.fake(pending_delay=2)

        def exact_checkpoint(name: str) -> None:
            if name == "before-runner-prerequisite-post-attempt-gate":
                exact_approval.approved = True

        with mock.patch.object(
            approval, "_checkpoint", side_effect=exact_checkpoint
        ):
            with self.assertRaisesRegex(
                approval.ApprovalFailure,
                "runner-prerequisite-unarmed-review-present",
            ):
                self.approve(exact_approval)
        self.assertEqual(exact_approval.posts, 0)
        self.assertFalse(
            approval._post_attempt_path(self.reservation_raw).exists()
        )

        self.tearDown()
        self.setUp()
        material = self.fake()

        def material_checkpoint(name: str) -> None:
            if name == "before-runner-prerequisite-post-attempt-gate":
                self.write_materialization_claim(pending=True)

        with mock.patch.object(
            approval, "_checkpoint", side_effect=material_checkpoint
        ):
            with self.assertRaisesRegex(
                approval.ApprovalFailure,
                "runner-governed-materialization-present",
            ):
                self.approve(material)
        self.assertEqual(material.posts, 0)

    def test_dynamic_job_identity_and_complete_run_index_are_required(
        self,
    ) -> None:
        wrong_label = self.fake()
        wrong_label.job_name_override = approval._prerequisite_job_name(
            runner_label="pqrelease-" + "f" * 32,
            reservation_sha256=approval._digest(self.reservation_raw),
        )
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-dispatch-selection-invalid",
        ):
            self.approve(wrong_label)
        self.assertEqual(wrong_label.posts, 0)

        self.tearDown()
        self.setUp()
        wrong_ticket = self.fake()
        wrong_ticket.job_name_override = approval._prerequisite_job_name(
            runner_label=str(self.reservation_payload["runner_label"]),
            reservation_sha256="sha256:" + "f" * 64,
        )
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-dispatch-selection-invalid",
        ):
            self.approve(wrong_ticket)
        self.assertEqual(wrong_ticket.posts, 0)

        self.tearDown()
        self.setUp()
        many_other_shas = self.fake(other_sha_run_count=101)
        result = self.approve(many_other_shas)
        self.assertEqual(result["disposition"], "approved")
        self.assertEqual(many_other_shas.posts, 1)
        self.assertTrue(
            any(
                f"head_sha={self.reservation_payload['workflow_sha']}"
                in path
                for method, path in many_other_shas.calls
                if method == "GET"
            )
        )

        self.tearDown()
        self.setUp()
        truncated = self.fake(same_sha_total_count_extra=100)
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-run-index-invalid",
        ):
            self.approve(truncated)
        self.assertEqual(truncated.posts, 0)

    def test_frozen_v2_is_retirement_only_and_can_retire_after_expiry(
        self,
    ) -> None:
        github = self.fake()
        self.discover_intent(github)
        self.convert_intent_to_frozen_v2()
        calls_before = len(github.calls)
        with (
            mock.patch.object(
                approval,
                "_read_admin_token",
                side_effect=AssertionError(
                    "legacy rejection consumed the token descriptor"
                ),
            ),
            self.assertRaisesRegex(
                approval.ApprovalFailure,
                "runner-prerequisite-legacy-v2-nondispatchable",
            ),
        ):
            approval.approve(now=1_900_000_005)
        self.assertEqual(len(github.calls), calls_before)

        expires = self.reservation_payload["expires_at_epoch"]
        self.assertIsInstance(expires, int)
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-discovered-terminal-required",
        ):
            approval.reservation.recover_expired(now=expires + 1)
        github.job_name_override = approval.PREREQUISITE_JOB_KEY
        github.approved = True
        github.duplicate_reviews = True
        github.prerequisite_conclusion = "success"
        github.terminal = True
        retired = self.retire(github, now=expires + 2)
        self.assertEqual(
            retired["disposition"], "terminal-adopted-get-only"
        )
        retirement = approval._verify_wire(
            approval._retirement_terminal_path(
                self.reservation_raw
            ).read_bytes(),
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.RETIREMENT_TERMINAL_SCHEMA,
            domain=approval.RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        )
        self.assertEqual(retirement["prerequisite_conclusion"], "success")
        self.assertEqual(retirement["final_exact_review_match_count"], 2)
        repeated = approval.retire_terminal(
            now=expires + 3,
            requester=lambda *_args: (_ for _ in ()).throw(
                AssertionError("idempotent retirement reached GitHub")
            ),
        )
        self.assertEqual(
            repeated["disposition"], "already-terminal-adopted"
        )
        abandoned = approval.reservation.abandon_terminal(
            now=expires + 4
        )
        self.assertEqual(
            abandoned["disposition"], "abandoned-terminal-published"
        )
        self.assertFalse(self.reservation_root.exists())

    def test_unarmed_external_review_success_can_retire_terminal(self) -> None:
        github = self.fake()
        self.discover_intent(github)
        github.approved = True
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-unarmed-review-present",
        ):
            self.approve(github)
        self.assertEqual(github.posts, 0)
        self.assertFalse(
            approval._post_attempt_path(self.reservation_raw).exists()
        )

        github.prerequisite_conclusion = "success"
        github.terminal = True
        retired = self.retire(github)
        self.assertEqual(
            retired["disposition"], "terminal-adopted-get-only"
        )
        retirement = approval._verify_wire(
            approval._retirement_terminal_path(
                self.reservation_raw
            ).read_bytes(),
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.RETIREMENT_TERMINAL_SCHEMA,
            domain=approval.RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        )
        self.assertEqual(retirement["prerequisite_conclusion"], "success")
        self.assertFalse(retirement["approval_post_attempt_present"])
        self.assertEqual(retirement["final_exact_review_match_count"], 1)
        abandoned = approval.reservation.abandon_terminal(
            now=1_900_000_020
        )
        self.assertEqual(
            abandoned["disposition"], "abandoned-terminal-published"
        )

    def test_armed_approval_can_retire_get_only_after_remote_terminal(
        self,
    ) -> None:
        github = self.fake()

        def checkpoint(name: str) -> None:
            if name == "after-runner-prerequisite-post-attempt":
                raise SimulatedCrash(name)

        with mock.patch.object(approval, "_checkpoint", side_effect=checkpoint):
            with self.assertRaises(SimulatedCrash):
                self.approve(github)
        self.assertEqual(github.posts, 0)
        github.terminal = True
        retired = self.retire(github)
        self.assertEqual(
            retired["disposition"], "terminal-adopted-get-only"
        )
        retirement = approval._verify_wire(
            approval._retirement_terminal_path(
                self.reservation_raw
            ).read_bytes(),
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.RETIREMENT_TERMINAL_SCHEMA,
            domain=approval.RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        )
        self.assertTrue(retirement["approval_post_attempt_present"])
        self.assertEqual(
            retirement["approval_post_attempt_sha256"],
            approval._digest(
                approval._post_attempt_path(
                    self.reservation_raw
                ).read_bytes()
            ),
        )

        self.tearDown()
        self.setUp()
        after_post = self.fake()

        def post_checkpoint(name: str) -> None:
            if name == "after-runner-prerequisite-approval-post":
                raise SimulatedCrash(name)

        with mock.patch.object(
            approval, "_checkpoint", side_effect=post_checkpoint
        ):
            with self.assertRaises(SimulatedCrash):
                self.approve(after_post)
        self.assertEqual(after_post.posts, 1)
        after_post.terminal = True
        retired_after_post = self.retire(after_post)
        self.assertEqual(
            retired_after_post["disposition"],
            "terminal-adopted-get-only",
        )

    def test_retirement_rejects_attempt_drift_execution_and_material(
        self,
    ) -> None:
        drift = self.fake()
        self.discover_intent(drift)
        drift.terminal = True
        drift.current_run_attempt = 2
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-prerequisite-run-binding-invalid",
        ):
            self.retire(drift)

        self.tearDown()
        self.setUp()
        executed = self.fake()
        self.discover_intent(executed)
        executed.terminal = True
        executed.release_job_mode = "executed"
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(executed)

        self.tearDown()
        self.setUp()
        materialized = self.fake()
        self.discover_intent(materialized)
        materialized.terminal = True
        self.write_materialization_binding(pending=True)
        calls_before = len(materialized.calls)
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-governed-materialization-present",
        ):
            self.retire(materialized)
        self.assertEqual(len(materialized.calls), calls_before)

        self.tearDown()
        self.setUp()
        approved = self.fake()
        self.approve(approved)
        approved.terminal = True
        calls_before = len(approved.calls)
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-prerequisite-approval-present",
        ):
            self.retire(approved)
        self.assertEqual(len(approved.calls), calls_before)

    def test_retirement_accepts_only_inert_terminal_timestamp_stamp(
        self,
    ) -> None:
        stamped = self.fake()
        self.discover_intent(stamped)
        stamped.terminal = True
        stamped.release_job_mode = "inert-stamped"
        retired = self.retire(stamped)
        self.assertEqual(
            retired["disposition"], "terminal-adopted-get-only"
        )
        retirement = approval._verify_wire(
            approval._retirement_terminal_path(
                self.reservation_raw
            ).read_bytes(),
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.RETIREMENT_TERMINAL_SCHEMA,
            domain=approval.RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        )
        self.assertEqual(
            retirement["release_job_started_at"],
            retirement["release_job_completed_at"],
        )
        approval.reservation._validate_retirement_terminal(
            retirement,
            intent_raw=approval._read_record(
                approval._record_paths(self.reservation_raw)[0]
            ),
            intent=approval._verify_wire(
                approval._record_paths(self.reservation_raw)[0].read_bytes(),
                public=self.private.public_key(),
                key_id=self.key_id,
                schema=approval.INTENT_SCHEMA,
                domain=approval.INTENT_SIGNATURE_DOMAIN,
            ),
            post_attempt_raw=None,
        )

        self.tearDown()
        self.setUp()
        started = self.fake()
        self.discover_intent(started)
        started.terminal = True
        started.release_job_mode = "inert-started"
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(started)

        self.tearDown()
        self.setUp()
        wrong_labels = self.fake()
        self.discover_intent(wrong_labels)
        wrong_labels.terminal = True
        wrong_labels.release_job_mode = "inert-stamped"
        wrong_labels.release_job_labels_override = [
            "propertyquarry-release-controller-v2"
        ]
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(wrong_labels)

    def test_retirement_accepts_unevaluated_labels_only_after_failed_prerequisite(
        self,
    ) -> None:
        def arm_post_attempt(github: FakeGitHub) -> None:
            def checkpoint(name: str) -> None:
                if name == "after-runner-prerequisite-post-attempt":
                    raise SimulatedCrash(name)

            with mock.patch.object(
                approval, "_checkpoint", side_effect=checkpoint
            ):
                with self.assertRaises(SimulatedCrash):
                    self.approve(github)
            self.assertEqual(github.posts, 0)

        def configure_unevaluated(
            github: FakeGitHub, *, arm: bool = True
        ) -> None:
            if arm:
                arm_post_attempt(github)
            else:
                self.discover_intent(github)
            github.terminal = True
            github.prerequisite_conclusion = "failure"
            github.release_job_mode = "inert-stamped"
            github.release_job_labels_override = []

        def mutate_release_job(
            github: FakeGitHub, **updates: object
        ) -> None:
            original_jobs = github.jobs

            def mutated_jobs(run_id: int) -> dict[str, object]:
                value = original_jobs(run_id)
                for job in value["jobs"]:
                    if job.get("name") == approval.RELEASE_JOB:
                        job.update(updates)
                return value

            github.jobs = mutated_jobs  # type: ignore[method-assign]

        unevaluated = self.fake()
        configure_unevaluated(unevaluated)
        retired = self.retire(unevaluated)
        self.assertEqual(
            retired["disposition"], "terminal-adopted-get-only"
        )
        terminal = approval._verify_wire(
            approval._retirement_terminal_path(
                self.reservation_raw
            ).read_bytes(),
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.RETIREMENT_TERMINAL_SCHEMA,
            domain=approval.RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        )
        self.assertEqual(terminal["release_job_labels"], [])
        self.assertEqual(terminal["prerequisite_conclusion"], "failure")
        approval.reservation._validate_retirement_terminal(
            terminal,
            intent_raw=approval._read_record(
                approval._record_paths(self.reservation_raw)[0]
            ),
            intent=approval._verify_wire(
                approval._record_paths(self.reservation_raw)[0].read_bytes(),
                public=self.private.public_key(),
                key_id=self.key_id,
                schema=approval.INTENT_SCHEMA,
                domain=approval.INTENT_SIGNATURE_DOMAIN,
            ),
            post_attempt_raw=approval._post_attempt_path(
                self.reservation_raw
            ).read_bytes(),
        )
        forged_without_post_attempt = json.loads(
            json.dumps(terminal)
        )
        forged_without_post_attempt["approval_post_attempt_present"] = False
        forged_without_post_attempt["approval_post_attempt_sha256"] = None
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-retirement-terminal-invalid",
        ):
            approval.reservation._validate_retirement_terminal(
                forged_without_post_attempt,
                intent_raw=approval._read_record(
                    approval._record_paths(self.reservation_raw)[0]
                ),
                intent=approval._verify_wire(
                    approval._record_paths(
                        self.reservation_raw
                    )[0].read_bytes(),
                    public=self.private.public_key(),
                    key_id=self.key_id,
                    schema=approval.INTENT_SCHEMA,
                    domain=approval.INTENT_SIGNATURE_DOMAIN,
                ),
                post_attempt_raw=None,
            )
        forged_invalid_timestamp = json.loads(json.dumps(terminal))
        forged_invalid_timestamp["release_job_started_at"] = (
            "not-a-timestamp"
        )
        forged_invalid_timestamp["release_job_completed_at"] = (
            "not-a-timestamp"
        )
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-retirement-terminal-invalid",
        ):
            approval.reservation._validate_retirement_terminal(
                forged_invalid_timestamp,
                intent_raw=approval._read_record(
                    approval._record_paths(self.reservation_raw)[0]
                ),
                intent=approval._verify_wire(
                    approval._record_paths(
                        self.reservation_raw
                    )[0].read_bytes(),
                    public=self.private.public_key(),
                    key_id=self.key_id,
                    schema=approval.INTENT_SCHEMA,
                    domain=approval.INTENT_SIGNATURE_DOMAIN,
                ),
                post_attempt_raw=approval._post_attempt_path(
                    self.reservation_raw
                ).read_bytes(),
            )
        abandoned = approval.reservation.abandon_terminal(
            now=1_900_000_020
        )
        self.assertEqual(
            abandoned["disposition"], "abandoned-terminal-published"
        )

        self.tearDown()
        self.setUp()
        unarmed = self.fake()
        configure_unevaluated(unarmed, arm=False)
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(unarmed)

        self.tearDown()
        self.setUp()
        cancelled_prerequisite = self.fake()
        configure_unevaluated(cancelled_prerequisite)
        cancelled_prerequisite.prerequisite_conclusion = "cancelled"
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(cancelled_prerequisite)

        self.tearDown()
        self.setUp()
        failed_run = self.fake()
        configure_unevaluated(failed_run)
        failed_run.run_conclusion = "failure"
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(failed_run)

        self.tearDown()
        self.setUp()
        skipped_release = self.fake()
        configure_unevaluated(skipped_release)
        mutate_release_job(skipped_release, conclusion="skipped")
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(skipped_release)

        self.tearDown()
        self.setUp()
        grouped_release = self.fake()
        configure_unevaluated(grouped_release)
        mutate_release_job(
            grouped_release,
            runner_group_id=17,
            runner_group_name="unexpected-group",
        )
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(grouped_release)

        self.tearDown()
        self.setUp()
        unstamped = self.fake()
        configure_unevaluated(unstamped)
        unstamped.release_job_mode = "inert"
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(unstamped)

        self.tearDown()
        self.setUp()
        successful_prerequisite = self.fake()
        configure_unevaluated(successful_prerequisite)
        successful_prerequisite.prerequisite_conclusion = "success"
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-release-job-executed",
        ):
            self.retire(successful_prerequisite)

    def test_retirement_requires_double_stable_terminal_observation(
        self,
    ) -> None:
        github = self.fake()
        self.discover_intent(github)
        github.terminal = True
        original_jobs = github.jobs
        reads = 0

        def drifting_jobs(run_id: int):
            nonlocal reads
            reads += 1
            value = original_jobs(run_id)
            if reads == 2:
                value["jobs"][0]["updated_at"] = "2030-03-17T18:00:01Z"
            return value

        github.jobs = drifting_jobs  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            approval.ApprovalFailure,
            "runner-retirement-observation-drift",
        ):
            self.retire(github)

    def test_only_untouched_prepared_reservation_can_expire(self) -> None:
        expires = self.reservation_payload["expires_at_epoch"]
        self.assertIsInstance(expires, int)
        expired = approval.reservation.recover_expired(now=expires + 1)
        self.assertEqual(
            expired["disposition"], "expired-terminal-published"
        )
        self.assertFalse(self.reservation_root.exists())

        self.tearDown()
        self.setUp()
        github = self.fake()
        self.discover_intent(github)
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-discovered-terminal-required",
        ):
            approval.reservation.recover_expired(
                now=self.reservation_payload["expires_at_epoch"] + 1
            )
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-governed-transition-nondispatchable",
        ):
            approval.reservation.prepare(
                now=1_900_000_020,
                source_observer=lambda: (_ for _ in ()).throw(
                    AssertionError("discovered prepare observed source")
                ),
                source_validator=lambda _payload: (_ for _ in ()).throw(
                    AssertionError("discovered prepare validated source")
                ),
            )

    def test_expired_terminal_rejects_every_materialization_record_form(
        self,
    ) -> None:
        cases = (
            ("write_materialization_claim", False),
            ("write_materialization_claim", True),
            ("write_materialization_binding", False),
            ("write_materialization_binding", True),
        )
        for index, (writer_name, pending) in enumerate(cases):
            with self.subTest(record=writer_name, pending=pending):
                getattr(self, writer_name)(pending=pending)
                self.publish_expired_terminal()
                with self.assertRaisesRegex(
                    approval.reservation.ReservationFailure,
                    "runner-reservation-expired-materialization-present",
                ):
                    approval.reservation.prepare(
                        now=(
                            int(self.reservation_payload["expires_at_epoch"])
                            + 1
                        ),
                        source_observer=lambda: (_ for _ in ()).throw(
                            AssertionError(
                                "expired materialization observed source"
                            )
                        ),
                    )
            if index != len(cases) - 1:
                self.tearDown()
                self.setUp()

    def test_expired_terminal_rejects_v2_intent_and_v3_approval_state(
        self,
    ) -> None:
        legacy = self.fake()
        self.discover_intent(legacy)
        self.convert_intent_to_frozen_v2()
        self.publish_expired_terminal()
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-expired-governed-state-present",
        ):
            approval.reservation.prepare(
                now=int(self.reservation_payload["expires_at_epoch"]) + 1,
                source_observer=lambda: (_ for _ in ()).throw(
                    AssertionError("expired v2 intent observed source")
                ),
            )

        self.tearDown()
        self.setUp()
        approved = self.fake()
        self.approve(approved)
        self.publish_expired_terminal()
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-expired-governed-state-present",
        ):
            approval.reservation.prepare(
                now=int(self.reservation_payload["expires_at_epoch"]) + 1,
                source_observer=lambda: (_ for _ in ()).throw(
                    AssertionError("expired v3 approval observed source")
                ),
            )

    def test_expired_and_abandoned_terminal_duplicate_is_rejected(
        self,
    ) -> None:
        self.publish_expired_terminal()
        duplicate = self.terminal_root / (
            approval._digest(self.reservation_raw).removeprefix("sha256:")
            + ".abandoned.v2"
        )
        duplicate.mkdir(mode=0o700)
        duplicate_ticket = duplicate / approval.reservation.RESERVATION_NAME
        duplicate_ticket.write_bytes(self.reservation_raw)
        os.chmod(duplicate_ticket, 0o600)
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-terminal-state-conflict",
        ):
            approval.reservation.prepare(
                now=int(self.reservation_payload["expires_at_epoch"]) + 1,
                source_observer=lambda: (_ for _ in ()).throw(
                    AssertionError("duplicate terminal observed source")
                ),
            )

    def test_duplicate_expiry_removal_recovers_full_and_empty_tombstones(
        self,
    ) -> None:
        checkpoints = (
            "after-runner-reservation-removal-tombstone",
            "after-runner-reservation-removal-ticket-unlink",
        )
        for index, crash_boundary in enumerate(checkpoints):
            expires = int(self.reservation_payload["expires_at_epoch"])
            approval.reservation.recover_expired(now=expires + 1)
            self.restore_active_duplicate()

            def checkpoint(name: str) -> None:
                if name == crash_boundary:
                    raise SimulatedCrash(name)

            with mock.patch.object(
                approval.reservation,
                "_checkpoint",
                side_effect=checkpoint,
            ):
                with self.assertRaisesRegex(
                    SimulatedCrash, crash_boundary
                ):
                    approval.reservation.recover_expired(now=expires + 2)
            self.assertFalse(self.reservation_root.exists())
            self.assertTrue(self.removal_tombstone().exists())
            if crash_boundary.endswith("ticket-unlink"):
                self.assertEqual(
                    list(self.removal_tombstone().iterdir()), []
                )
            else:
                self.assertEqual(
                    [
                        item.name
                        for item in self.removal_tombstone().iterdir()
                    ],
                    [approval.reservation.RESERVATION_NAME],
                )
            recovered = approval.reservation.recover_expired(
                now=expires + 3
            )
            self.assertEqual(
                recovered["disposition"],
                "expired-terminal-already-published",
            )
            self.assertFalse(self.removal_tombstone().exists())
            if index != len(checkpoints) - 1:
                self.tearDown()
                self.setUp()

    def test_duplicate_abandonment_removal_recovers_empty_tombstone(
        self,
    ) -> None:
        github = self.fake()
        self.discover_intent(github)
        github.terminal = True
        self.retire(github)
        approval.reservation.abandon_terminal(now=1_900_000_020)
        self.restore_active_duplicate()

        def checkpoint(name: str) -> None:
            if name == "after-runner-reservation-removal-ticket-unlink":
                raise SimulatedCrash(name)

        with mock.patch.object(
            approval.reservation,
            "_checkpoint",
            side_effect=checkpoint,
        ):
            with self.assertRaisesRegex(
                SimulatedCrash,
                "after-runner-reservation-removal-ticket-unlink",
            ):
                approval.reservation.abandon_terminal(now=1_900_000_021)
        self.assertFalse(self.reservation_root.exists())
        self.assertEqual(list(self.removal_tombstone().iterdir()), [])
        recovered = approval.reservation.abandon_terminal(
            now=1_900_000_022
        )
        self.assertEqual(
            recovered["disposition"],
            "abandoned-terminal-already-published",
        )
        self.assertFalse(self.removal_tombstone().exists())

    def test_removal_tombstone_rejects_unknown_content_and_metadata(
        self,
    ) -> None:
        expires = int(self.reservation_payload["expires_at_epoch"])
        approval.reservation.recover_expired(now=expires + 1)
        tombstone = self.removal_tombstone()
        tombstone.mkdir(mode=0o700)
        unknown = tombstone / "unexpected"
        unknown.write_bytes(b"tamper")
        os.chmod(unknown, 0o600)
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-removal-tombstone-invalid",
        ):
            approval.reservation.recover_expired(now=expires + 2)

        unknown.unlink()
        os.chmod(tombstone, 0o755)
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-path-metadata-invalid",
        ):
            approval.reservation.recover_expired(now=expires + 3)

    def test_abandonment_crash_recovery_and_prepare_after_terminal(self) -> None:
        github = self.fake()
        self.discover_intent(github)
        github.approved = True
        github.duplicate_reviews = True
        github.release_job_mode = "inert"
        github.terminal = True
        retired = self.retire(github)
        self.assertEqual(
            retired["disposition"], "terminal-adopted-get-only"
        )
        retirement_raw = approval._retirement_terminal_path(
            self.reservation_raw
        ).read_bytes()
        retirement = approval._verify_wire(
            retirement_raw,
            public=self.private.public_key(),
            key_id=self.key_id,
            schema=approval.RETIREMENT_TERMINAL_SCHEMA,
            domain=approval.RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        )
        self.assertEqual(retirement["final_review_match_count"], 2)
        self.assertEqual(
            retirement["release_job_disposition"], "inert-terminal"
        )

        def checkpoint(name: str) -> None:
            if name == "after-runner-reservation-abandonment-record":
                raise SimulatedCrash(name)

        with mock.patch.object(
            approval.reservation, "_checkpoint", side_effect=checkpoint
        ):
            with self.assertRaises(SimulatedCrash):
                approval.reservation.abandon_terminal(now=1_900_000_020)
        self.assertTrue(self.reservation_root.exists())
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-abandonment-recovery-required",
        ):
            approval.reservation.prepare(
                now=1_900_000_021,
                source_observer=lambda: (_ for _ in ()).throw(
                    AssertionError("prepare rebuilt before abandonment")
                ),
                source_validator=lambda payload: {},
            )

        recovered = approval.reservation.abandon_terminal(
            now=1_900_000_022
        )
        self.assertEqual(
            recovered["disposition"], "abandoned-terminal-recovered"
        )
        self.assertFalse(self.reservation_root.exists())
        self.assertTrue(Path(recovered["terminal_path"]).exists())
        repeated = approval.reservation.abandon_terminal(
            now=1_900_000_023
        )
        self.assertEqual(
            repeated["disposition"],
            "abandoned-terminal-already-published",
        )

        source = {
            "source_checkout_identity_sha256": "sha256:" + "b" * 64,
            "source_checkout_path": os.fspath(
                self.checkout_root / ("a" * 40)
            ),
            "source_tree_sha256": "sha256:" + "c" * 64,
            "workflow_sha": "a" * 40,
        }
        fresh = approval.reservation.prepare(
            now=1_900_000_024,
            random_source=lambda size: bytes(reversed(range(size))),
            source_observer=lambda: dict(source),
            source_validator=lambda payload: dict(source),
        )
        self.assertEqual(fresh["disposition"], "prepared")
        self.assertNotEqual(
            fresh["dispatch_ticket_sha256"],
            approval._digest(self.reservation_raw),
        )

    def test_abandonment_rejects_approval_and_materialization_conflicts(
        self,
    ) -> None:
        github = self.fake()
        self.discover_intent(github)
        github.terminal = True
        self.retire(github)
        _intent_path, approval_path = approval._record_paths(
            self.reservation_raw
        )
        approval_path.write_bytes(b"conflicting-approval")
        os.chmod(approval_path, 0o600)
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-abandonment-approval-present",
        ):
            approval.reservation.abandon_terminal(now=1_900_000_020)

        approval_path.unlink()
        self.write_materialization_claim(pending=True)
        with self.assertRaisesRegex(
            approval.reservation.ReservationFailure,
            "runner-reservation-abandonment-materialization-present",
        ):
            approval.reservation.abandon_terminal(now=1_900_000_020)

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
        self.assertIn(
            '    name: "propertyquarry-protected-dispatch-inputs | '
            '${{ inputs.release_runner_label }} | '
            '${{ inputs.release_runner_ticket_sha256 }}"\n',
            prerequisite,
        )
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
