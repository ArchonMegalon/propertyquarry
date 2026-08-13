from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from scripts import propertyquarry_deploy_journal as journal


RELEASE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
TOPOLOGY_SHA256 = "c" * 64


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "release-control" / "deploy-journal.json"
    monkeypatch.setattr(journal, "JOURNAL_PATH", path)
    return path


def _record(deployment_id: str, phase: str) -> dict[str, object]:
    return journal.record_phase(
        deployment_id=deployment_id,
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        writer_topology_sha256=TOPOLOGY_SHA256,
        phase=phase,
    )


def test_journal_is_private_durable_candidate_bound_and_monotonic(
    isolated_journal: Path,
) -> None:
    _record("deploy-one", "armed")
    _record("deploy-one", "writers_quiesced")

    assert isolated_journal.stat().st_mode & 0o777 == 0o600
    assert journal.load_journal()["phase"] == "writers_quiesced"  # type: ignore[index]
    with pytest.raises(journal.DeployJournalError, match="advance monotonically"):
        _record("deploy-one", "armed")
    with pytest.raises(journal.DeployJournalError, match="incomplete prior deployment"):
        _record("deploy-two", "armed")


@pytest.mark.parametrize(
    "crash_phase",
    [
        "migration_committed",
        "proofs_running",
        "receipt_consumed",
        "ingress_starting",
        "public_verified",
    ],
)
def test_sigkill_window_persists_reconciliation_requirement(
    isolated_journal: Path,
    crash_phase: str,
) -> None:
    phases = list(journal.PHASE_ORDER)
    for phase in phases:
        if phase in {"contained", "promotion_complete"}:
            continue
        _record("deploy-crash", phase)
        if phase == crash_phase:
            break

    pid = os.fork()
    if pid == 0:
        os.kill(os.getpid(), signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status)
    assert journal.load_journal()["phase"] == crash_phase  # type: ignore[index]
    with pytest.raises(journal.DeployJournalError, match="incomplete prior deployment"):
        _record("deploy-next", "armed")

    journal.mark_contained()
    assert _record("deploy-next", "armed")["phase"] == "armed"


def test_corrupt_or_truncated_journal_fails_closed(isolated_journal: Path) -> None:
    isolated_journal.parent.mkdir(parents=True)
    isolated_journal.write_text('{"schema":', encoding="utf-8")

    with pytest.raises(journal.DeployJournalError, match="corrupt"):
        journal.load_journal()
    with pytest.raises(journal.DeployJournalError, match="corrupt"):
        _record("deploy-next", "armed")


def test_post_promotion_failure_can_replace_completion_with_verified_containment(
    isolated_journal: Path,
) -> None:
    for phase in journal.PHASE_ORDER:
        if phase == "contained":
            continue
        _record("deploy-contained-after-promotion", phase)

    assert journal.load_journal()["phase"] == "promotion_complete"  # type: ignore[index]
    assert journal.mark_contained()["phase"] == "contained"
    assert journal.load_journal()["phase"] == "contained"  # type: ignore[index]


def test_fixed_controller_flock_serializes_distinct_deployments(tmp_path: Path) -> None:
    lock_path = tmp_path / "deploy-controller.lock"
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            'exec 9>"$1"; flock -n 9; printf ready; read -r _',
            "bash",
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.read(5) == "ready"
    contender = subprocess.run(
        ["flock", "-n", str(lock_path), "true"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert contender.returncode != 0
    assert holder.stdin is not None
    holder.stdin.write("done\n")
    holder.stdin.flush()
    holder.wait(timeout=10)


def test_local_deploy_wrapper_has_no_retired_external_controller_surface() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts/deploy_propertyquarry.sh").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "record_deploy_journal_phase",
        "propertyquarry_finish_schema_quiesce",
        "propertyquarry_deploy_journal.py",
        "flock -n",
        "--controller-owns-all-privileged-actions",
        "--contain-before-candidate-validation",
        "--require-external-monotonic-cas",
        "--forbid-candidate-output-authority",
    ):
        assert forbidden not in source
    assert "Authoritative local-Docker PropertyQuarry deployment." in source
    assert "/usr/bin/docker compose" in source
    assert "scripts/propertyquarry_local_deployment_receipt.py" in source


def test_local_wrapper_validates_candidate_before_runtime_mutation_and_receipt() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts/deploy_propertyquarry.sh").read_text(
        encoding="utf-8"
    )
    repository_validation = source.index("python3 scripts/check_property_repository_role.py")
    manifest_validation = source.index("manifest_authority=")
    compose_validation = source.index("config --quiet")
    preflight_exit = source.index("if ((PREFLIGHT_ONLY == 1))")
    runtime_mutation = source.index("up --detach --wait --wait-timeout 120 propertyquarry-db")
    admission_provisioning = source.index("python3 scripts/provision_propertyquarry_admission_database.py")
    deployment_receipt = source.index("python3 scripts/propertyquarry_local_deployment_receipt.py")

    assert repository_validation < manifest_validation < compose_validation
    assert compose_validation < preflight_exit < runtime_mutation
    assert runtime_mutation < admission_provisioning < deployment_receipt
