from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.check_property_mirror_role import CANONICAL_URL, MIRROR_URL
from scripts.verify_generated_release_artifacts_clean import (
    RELEASE_ARTIFACT_SET_PREFIX,
    RELEASE_MANIFEST_JSON_END,
    RELEASE_MANIFEST_JSON_START,
    RELEASE_MANIFEST_STATIC_VALUES,
)


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_property_mirror_role.py"


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
        env=_git_env(),
    )
    return result.stdout.strip()


def _manifest(*, repository: str = "ArchonMegalon/propertyquarry") -> str:
    values = dict(RELEASE_MANIFEST_STATIC_VALUES)
    values["release_repository"] = repository
    values.update(
        {
            "release_commit_sha": "a" * 40,
            "release_artifact_set": f"{RELEASE_ARTIFACT_SET_PREFIX}{'b' * 64}",
            "release_label": f"propertyquarry-source-browser-candidate-{'a' * 12}",
            "release_generated_at": "2026-07-18T00:00:00Z",
            "release_deployment_id": f"propertyquarry-governed-deploy-{'a' * 12}",
        }
    )
    return (
        "# PropertyQuarry Release Manifest\n\n"
        f"{RELEASE_MANIFEST_JSON_START}\n"
        "```json\n"
        f"{json.dumps(values, indent=2, sort_keys=True)}\n"
        "```\n"
        f"{RELEASE_MANIFEST_JSON_END}\n"
    )


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _set_refs(repo: Path, canonical_sha: str, mirror_sha: str) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/main", canonical_sha)
    _git(repo, "update-ref", "refs/remotes/propertyquarry/main", mirror_sha)


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "mirror-role@example.invalid")
    _git(repo, "config", "user.name", "Mirror Role Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", CANONICAL_URL)
    _git(repo, "remote", "add", "propertyquarry", MIRROR_URL)
    manifest = repo / "docs" / "PROPERTYQUARRY_RELEASE_MANIFEST.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_manifest(), encoding="utf-8")
    (repo / "README.md").write_text("PropertyQuarry\n", encoding="utf-8")
    initial_sha = _commit(repo, "initial mirror")
    _set_refs(repo, initial_sha, initial_sha)
    return repo, initial_sha


def _run_gate(
    repo: Path,
    tmp_path: Path,
    *extra: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    receipt = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--repo-root",
            str(repo),
            "--write",
            str(receipt),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return result, json.loads(receipt.read_text(encoding="utf-8"))


def test_exact_canonical_and_mirror_commit_passes(tmp_path: Path) -> None:
    repo, sha = _fixture_repo(tmp_path)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--expected-canonical-sha",
        sha,
        "--expected-mirror-sha",
        sha,
        "--require-single-worktree",
    )

    assert result.returncode == 0, result.stderr
    assert "same commit" in result.stdout
    assert receipt["passed"] is True
    assert receipt["topology"]["classification"] == "exact"
    assert receipt["topology"]["tree_equal"] is True
    assert receipt["fetch_performed_by_gate"] is False
    assert receipt["network_freshness_proven"] is False


def test_pull_request_candidate_can_be_exact_descendant_of_exact_main(
    tmp_path: Path,
) -> None:
    repo, main_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text(
        "reviewed pull request candidate\n",
        encoding="utf-8",
    )
    candidate_sha = _commit(repo, "pull request candidate")

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        candidate_sha,
        "--expected-candidate-sha",
        candidate_sha,
        "--candidate-validation-mode",
        "pull-request-descendant",
    )

    assert result.returncode == 0, result.stderr
    assert "exact expected commit" in result.stdout
    assert receipt["passed"] is True
    assert receipt["observation_mode"] == "pull_request_candidate"
    assert receipt["canonical"]["sha"] == main_sha
    assert receipt["mirror"]["sha"] == main_sha
    assert receipt["mirror_candidate"]["sha"] == candidate_sha
    assert receipt["main_topology"]["classification"] == "exact"
    assert receipt["topology"]["classification"] == "mirror_ahead"
    assert receipt["topology"]["mirror_ahead_by"] == 1
    assert receipt["policy"]["candidate_validation_mode"] == (
        "pull-request-descendant"
    )
    assert receipt["policy"]["require_exact_commit_identity"] is False
    assert receipt["policy"][
        "require_canonical_mirror_exact_commit_identity"
    ] is True
    assert receipt["policy"]["require_candidate_exact_canonical_identity"] is False
    assert receipt["policy"]["require_candidate_descends_from_canonical"] is True
    assert receipt["policy"]["require_candidate_exact_expected_sha"] is True


def test_descendant_candidate_requires_explicit_pull_request_mode(
    tmp_path: Path,
) -> None:
    repo, _ = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("unscoped candidate\n", encoding="utf-8")
    candidate_sha = _commit(repo, "unscoped candidate")

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        candidate_sha,
        "--expected-candidate-sha",
        candidate_sha,
    )

    assert result.returncode == 2
    assert receipt["observation_mode"] == "mirror_sync_pr_candidate"
    assert receipt["topology"]["classification"] == "mirror_ahead"
    assert "topology_not_exact:mirror_ahead" in receipt["failures"]


def test_pull_request_candidate_rejects_non_descendant_commit(
    tmp_path: Path,
) -> None:
    repo, main_sha = _fixture_repo(tmp_path)
    tree = _git(repo, "rev-parse", f"{main_sha}^{{tree}}")
    unrelated_sha = _git(
        repo,
        "commit-tree",
        tree,
        input_text="unrelated candidate\n",
    )

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        unrelated_sha,
        "--expected-candidate-sha",
        unrelated_sha,
        "--candidate-validation-mode",
        "pull-request-descendant",
    )

    assert result.returncode == 2
    assert receipt["main_topology"]["classification"] == "exact"
    assert receipt["topology"]["classification"] == "history_incomplete"
    assert "pull_request_candidate_not_descendant:history_incomplete" in receipt[
        "failures"
    ]


def test_pull_request_candidate_rejects_ancestor_of_current_main(
    tmp_path: Path,
) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("current main\n", encoding="utf-8")
    main_sha = _commit(repo, "advance main")
    _set_refs(repo, main_sha, main_sha)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        initial_sha,
        "--expected-candidate-sha",
        initial_sha,
        "--candidate-validation-mode",
        "pull-request-descendant",
    )

    assert result.returncode == 2
    assert receipt["main_topology"]["classification"] == "exact"
    assert receipt["topology"]["classification"] == "mirror_lagging"
    assert "pull_request_candidate_not_descendant:mirror_lagging" in receipt[
        "failures"
    ]


def test_pull_request_candidate_requires_exact_main_identity(
    tmp_path: Path,
) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("canonical advanced\n", encoding="utf-8")
    canonical_sha = _commit(repo, "canonical advance")
    _set_refs(repo, canonical_sha, initial_sha)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        canonical_sha,
        "--expected-candidate-sha",
        canonical_sha,
        "--candidate-validation-mode",
        "pull-request-descendant",
    )

    assert result.returncode == 2
    assert receipt["topology"]["classification"] == "exact"
    assert receipt["main_topology"]["classification"] == "mirror_lagging"
    assert "pull_request_main_topology_not_exact:mirror_lagging" in receipt[
        "failures"
    ]


def test_pull_request_candidate_requires_exact_expected_sha(
    tmp_path: Path,
) -> None:
    repo, main_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text(
        "candidate with wrong expectation\n",
        encoding="utf-8",
    )
    candidate_sha = _commit(repo, "candidate with wrong expectation")

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        candidate_sha,
        "--expected-candidate-sha",
        main_sha,
        "--candidate-validation-mode",
        "pull-request-descendant",
    )

    assert result.returncode == 2
    assert "mirror_candidate_expected_sha_mismatch" in receipt["failures"]


def test_pull_request_candidate_requires_valid_release_manifest_role(
    tmp_path: Path,
) -> None:
    repo, _ = _fixture_repo(tmp_path)
    manifest = repo / "docs" / "PROPERTYQUARRY_RELEASE_MANIFEST.md"
    manifest.write_text(
        _manifest(repository="ArchonMegalon/property"),
        encoding="utf-8",
    )
    candidate_sha = _commit(repo, "candidate with invalid release role")

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        candidate_sha,
        "--expected-candidate-sha",
        candidate_sha,
        "--candidate-validation-mode",
        "pull-request-descendant",
    )

    assert result.returncode == 2
    assert receipt["main_topology"]["classification"] == "exact"
    assert receipt["mirror_candidate"]["release_manifest_role_valid"] is False
    assert "mirror_candidate_release_manifest_invalid" in receipt["failures"]


def test_pull_request_mode_requires_candidate_ref(tmp_path: Path) -> None:
    repo, _ = _fixture_repo(tmp_path)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--candidate-validation-mode",
        "pull-request-descendant",
    )

    assert result.returncode == 2
    assert "pull_request_candidate_ref_required" in receipt["failures"]


def test_mirror_lag_is_reported_and_blocks(tmp_path: Path) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("canonical advanced\n", encoding="utf-8")
    canonical_sha = _commit(repo, "canonical advance")
    _set_refs(repo, canonical_sha, initial_sha)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["topology"]["classification"] == "mirror_lagging"
    assert receipt["topology"]["canonical_ahead_by"] == 1
    assert "topology_not_exact:mirror_lagging" in receipt["failures"]


def test_mirror_sync_pr_candidate_can_prove_safe_fast_forward(
    tmp_path: Path,
) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("canonical advanced\n", encoding="utf-8")
    canonical_sha = _commit(repo, "canonical advance")
    _set_refs(repo, canonical_sha, initial_sha)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        canonical_sha,
        "--expected-candidate-sha",
        canonical_sha,
    )

    assert result.returncode == 0, result.stderr
    assert "exact canonical fast-forward commit" in result.stdout
    assert receipt["passed"] is True
    assert receipt["observation_mode"] == "mirror_sync_pr_candidate"
    assert receipt["main_topology"]["classification"] == "mirror_lagging"
    assert receipt["topology"]["classification"] == "exact"
    assert receipt["mirror"]["sha"] == initial_sha
    assert receipt["mirror_candidate"]["sha"] == canonical_sha


def test_mirror_sync_pr_candidate_must_equal_canonical_commit(
    tmp_path: Path,
) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("canonical advanced\n", encoding="utf-8")
    canonical_sha = _commit(repo, "canonical advance")
    _set_refs(repo, canonical_sha, initial_sha)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        initial_sha,
        "--expected-candidate-sha",
        initial_sha,
    )

    assert result.returncode == 2
    assert receipt["topology"]["classification"] == "mirror_lagging"
    assert "topology_not_exact:mirror_lagging" in receipt["failures"]


def test_mirror_sync_pr_candidate_rejects_non_fast_forwardable_main(
    tmp_path: Path,
) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("canonical side\n", encoding="utf-8")
    canonical_sha = _commit(repo, "canonical side")
    _git(repo, "switch", "-q", "--detach", initial_sha)
    (repo / "README.md").write_text("mirror side\n", encoding="utf-8")
    mirror_sha = _commit(repo, "mirror side")
    _set_refs(repo, canonical_sha, mirror_sha)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--mirror-candidate-ref",
        canonical_sha,
        "--expected-candidate-sha",
        canonical_sha,
    )

    assert result.returncode == 2
    assert receipt["topology"]["classification"] == "exact"
    assert receipt["main_topology"]["classification"] == "diverged"
    assert "mirror_candidate_main_not_fast_forwardable:diverged" in receipt[
        "failures"
    ]


def test_mirror_ahead_is_reported_and_blocks(tmp_path: Path) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("mirror advanced\n", encoding="utf-8")
    mirror_sha = _commit(repo, "mirror advance")
    _set_refs(repo, initial_sha, mirror_sha)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["topology"]["classification"] == "mirror_ahead"
    assert receipt["topology"]["mirror_ahead_by"] == 1


def test_diverged_histories_are_reported_and_block(tmp_path: Path) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("canonical side\n", encoding="utf-8")
    canonical_sha = _commit(repo, "canonical side")
    _git(repo, "switch", "-q", "--detach", initial_sha)
    (repo / "README.md").write_text("mirror side\n", encoding="utf-8")
    mirror_sha = _commit(repo, "mirror side")
    _set_refs(repo, canonical_sha, mirror_sha)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["topology"]["classification"] == "diverged"
    assert receipt["topology"]["canonical_ahead_by"] == 1
    assert receipt["topology"]["mirror_ahead_by"] == 1


def test_same_tree_with_different_commit_identity_still_blocks(tmp_path: Path) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "envelope only")
    canonical_sha = _git(repo, "rev-parse", "HEAD")
    _set_refs(repo, canonical_sha, initial_sha)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["topology"]["tree_equal"] is True
    assert receipt["topology"]["exact_commit"] is False


def test_missing_common_history_fails_closed(tmp_path: Path) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    tree = _git(repo, "rev-parse", f"{initial_sha}^{{tree}}")
    unrelated_sha = _git(repo, "commit-tree", tree, input_text="unrelated root\n")
    _set_refs(repo, initial_sha, unrelated_sha)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["topology"]["classification"] == "history_incomplete"


@pytest.mark.parametrize("remote", ["origin", "propertyquarry"])
def test_wrong_remote_url_blocks_without_echoing_value(
    tmp_path: Path,
    remote: str,
) -> None:
    repo, _ = _fixture_repo(tmp_path)
    secretish_url = "https://user:do-not-print@example.invalid/repo.git"
    _git(repo, "remote", "set-url", remote, secretish_url)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert f"{remote}_fetch_url_mismatch" not in receipt["failures"]
    expected_failure = (
        "canonical_fetch_url_mismatch"
        if remote == "origin"
        else "mirror_fetch_url_mismatch"
    )
    assert expected_failure in receipt["failures"]
    assert secretish_url not in result.stderr
    assert secretish_url not in json.dumps(receipt)


def test_url_rewrite_configuration_blocks(tmp_path: Path) -> None:
    repo, _ = _fixture_repo(tmp_path)
    _git(
        repo,
        "config",
        "--local",
        "url.https://example.invalid/.insteadOf",
        "https://github.com/",
    )

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["policy"]["git_url_rewrite_count"] == 1
    assert "git_url_rewrite_configured" in receipt["failures"]


def test_multiple_registered_worktrees_block_when_ci_policy_requires_one(
    tmp_path: Path,
) -> None:
    repo, sha = _fixture_repo(tmp_path)
    _git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "linked"), sha)

    result, receipt = _run_gate(repo, tmp_path, "--require-single-worktree")

    assert result.returncode == 2
    assert receipt["worktrees"]["registered"] == 2
    assert "worktree_inventory_not_single_clean_checkout" in receipt["failures"]


def test_expected_sha_mismatch_blocks(tmp_path: Path) -> None:
    repo, _ = _fixture_repo(tmp_path)

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--expected-canonical-sha",
        "f" * 40,
    )

    assert result.returncode == 2
    assert "canonical_expected_sha_mismatch" in receipt["failures"]


def test_local_release_binding_rejects_dirty_checkout(tmp_path: Path) -> None:
    repo, _ = _fixture_repo(tmp_path)
    (repo / "untracked-release-note.txt").write_text("dirty\n", encoding="utf-8")

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--require-head-at-canonical",
        "--require-clean-worktree",
    )

    assert result.returncode == 2
    assert receipt["checkout"]["clean"] is False
    assert "worktree_not_clean" in receipt["failures"]


def test_local_release_binding_rejects_head_not_at_canonical(
    tmp_path: Path,
) -> None:
    repo, _ = _fixture_repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local envelope")

    result, receipt = _run_gate(
        repo,
        tmp_path,
        "--require-head-at-canonical",
    )

    assert result.returncode == 2
    assert receipt["checkout"]["head_sha"] != receipt["canonical"]["sha"]
    assert "worktree_head_not_canonical" in receipt["failures"]


def test_invalid_manifest_role_blocks_both_equal_refs(tmp_path: Path) -> None:
    repo, _ = _fixture_repo(tmp_path)
    manifest = repo / "docs" / "PROPERTYQUARRY_RELEASE_MANIFEST.md"
    manifest.write_text(_manifest(repository="ArchonMegalon/property"), encoding="utf-8")
    invalid_sha = _commit(repo, "invalid role")
    _set_refs(repo, invalid_sha, invalid_sha)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["canonical"]["release_manifest_role_valid"] is False
    assert receipt["mirror"]["release_manifest_role_valid"] is False


def test_nul_delimited_diff_preserves_newline_filename_as_one_path(
    tmp_path: Path,
) -> None:
    repo, initial_sha = _fixture_repo(tmp_path)
    odd_path = repo / "tests" / "odd\nname.py"
    odd_path.parent.mkdir()
    odd_path.write_text("assert True\n", encoding="utf-8")
    canonical_sha = _commit(repo, "newline filename")
    _set_refs(repo, canonical_sha, initial_sha)

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 2
    assert receipt["diff"]["changed_path_count"] == 1
    assert receipt["diff"]["changed_paths"] == ["tests/odd\nname.py"]
    assert receipt["diff"]["critical_paths"] == ["tests/odd\nname.py"]


def test_unresolvable_ref_is_operational_failure_with_receipt(tmp_path: Path) -> None:
    repo, _ = _fixture_repo(tmp_path)
    _git(repo, "update-ref", "-d", "refs/remotes/propertyquarry/main")

    result, receipt = _run_gate(repo, tmp_path)

    assert result.returncode == 1
    assert receipt["passed"] is False
    assert receipt["operational_errors"] == ["git_rev-parse_failed"]


def test_workflow_and_release_bundle_fail_closed_on_mirror_role() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "smoke-runtime.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    mirror_job = jobs["propertyquarry-mirror-role-contract"]
    assert mirror_job["permissions"] == {"contents": "read"}
    assert mirror_job["runs-on"] == "ubuntu-latest"
    checkout = mirror_job["steps"][0]
    assert checkout["uses"] == (
        "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"
    )
    assert checkout["with"] == {
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert mirror_job["steps"][1]["name"] == (
        "Initialize sanitized canonical/mirror evidence"
    )
    initialized_receipt = mirror_job["steps"][1]["run"]
    assert "propertyquarry.mirror_role.ci_preflight.v1" in initialized_receipt
    assert "ci_mirror_fetch_or_gate_not_completed" in initialized_receipt
    assert "RUNNER_TEMP" in initialized_receipt
    gate_run = mirror_job["steps"][2]["run"]
    assert "refusing mirror fetch while Git URL rewrite rules are configured" in gate_run
    assert "+refs/heads/main:refs/remotes/origin/main" in gate_run
    assert "+refs/heads/main:refs/remotes/propertyquarry/main" in gate_run
    assert "ArchonMegalon/propertyquarry" in gate_run
    assert "PROPERTYQUARRY_PR_HEAD_SHA" in gate_run
    assert "--mirror-candidate-ref" in gate_run
    assert "--expected-candidate-sha" in gate_run
    assert "--candidate-validation-mode pull-request-descendant" in gate_run
    pull_request_case = gate_run.index(
        "pull_request:ArchonMegalon/propertyquarry)"
    )
    candidate_mode = gate_run.index(
        "--candidate-validation-mode pull-request-descendant"
    )
    push_case = gate_run.index(
        "push:ArchonMegalon/propertyquarry|workflow_dispatch:"
        "ArchonMegalon/propertyquarry)"
    )
    assert pull_request_case < candidate_mode < push_case
    assert 'if [[ "${PROPERTYQUARRY_PR_BASE_REF}" != "main" ]]' in gate_run
    assert (
        "PropertyQuarry PR candidates must originate in "
        "ArchonMegalon/propertyquarry"
    ) in gate_run
    assert "scripts/check_property_mirror_role.py" in gate_run
    assert "--require-single-worktree" in gate_run
    assert '${RUNNER_TEMP}/propertyquarry_mirror_role/receipt.json' in gate_run
    assert "propertyquarry-mirror-role-contract" in jobs[
        "propertyquarry-ordinary-ci-success"
    ]["needs"]

    release_gate = (ROOT / "scripts" / "property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    assert "scripts/check_property_mirror_role.py" in release_gate
    assert "--require-head-at-canonical" in release_gate
    assert "--require-clean-worktree" in release_gate
    assert "--candidate-validation-mode" not in release_gate
    isolation = (ROOT / "docs" / "REPO_ISOLATION.md").read_text(encoding="utf-8")
    assert "`ArchonMegalon/propertyquarry` is the sole canonical" in isolation
    assert "network_freshness_proven: false" in isolation
    assert "review evidence only" in isolation
    assert "no commit from another repository can satisfy the gate" in isolation
    assert "non-authoritative and inert" in isolation

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`ArchonMegalon/propertyquarry`" in readme
    assert "sole canonical PropertyQuarry source and release repository" in readme
    assert "without release authority, commits, or runtime dependencies" in readme
