from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from scripts.check_property_mirror_role import (
    CANONICAL_REPOSITORY,
    CANONICAL_URL,
    MIRROR_REPOSITORY,
    MIRROR_URL,
)
from scripts.check_property_repository_role import (
    LEGACY_ALLOWED_WORKFLOW,
    LEGACY_FORBIDDEN_WORKFLOWS,
    POLICY_PATH,
    load_policy,
)
from scripts.verify_generated_release_artifacts_clean import (
    RELEASE_ARTIFACT_SET_PREFIX,
    RELEASE_MANIFEST_JSON_END,
    RELEASE_MANIFEST_JSON_START,
    RELEASE_MANIFEST_STATIC_VALUES,
)


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_property_repository_role.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    return result.stdout.strip()


def _manifest() -> str:
    values = dict(RELEASE_MANIFEST_STATIC_VALUES)
    values.update(
        {
            "release_commit_sha": "a" * 40,
            "release_artifact_set": f"{RELEASE_ARTIFACT_SET_PREFIX}{'b' * 64}",
            "release_label": f"propertyquarry-source-browser-candidate-{'a' * 12}",
            "release_generated_at": "2026-07-28T00:00:00Z",
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


def _fixture_repo(tmp_path: Path, *, role: str) -> Path:
    repo = tmp_path / role
    repo.mkdir()
    policy_target = repo / POLICY_PATH
    policy_target.parent.mkdir(parents=True)
    policy_target.write_bytes((ROOT / POLICY_PATH).read_bytes())
    manifest = repo / "docs" / "PROPERTYQUARRY_RELEASE_MANIFEST.md"
    manifest.parent.mkdir(parents=True)
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    if role == "canonical":
        manifest.write_text(_manifest(), encoding="utf-8")
        for path in LEGACY_FORBIDDEN_WORKFLOWS:
            target = repo / path
            target.write_text("name: canonical\n", encoding="utf-8")
        repository_url = CANONICAL_URL
    else:
        manifest.write_text(
            "# NONCANONICAL PropertyQuarry repository\n\n"
            "This legacy checkout has no release authority. The sole canonical "
            "repository is ArchonMegalon/propertyquarry.\n",
            encoding="utf-8",
        )
        (repo / LEGACY_ALLOWED_WORKFLOW).write_text(
            "name: propertyquarry-noncanonical\n",
            encoding="utf-8",
        )
        repository_url = MIRROR_URL
    (repo / "README.md").write_text(f"PropertyQuarry {role}\n", encoding="utf-8")
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "repository-role@example.invalid")
    _git(repo, "config", "user.name", "Repository Role Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", repository_url)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{role} fixture")
    return repo


def _run(
    repo: Path,
    tmp_path: Path,
    *,
    repository: str,
    role: str,
    require_clean: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    receipt = tmp_path / f"{role}-receipt.json"
    command = [
        sys.executable,
        str(GATE),
        "--repo-root",
        str(repo),
        "--expected-repository",
        repository,
        "--expected-role",
        role,
        "--write",
        str(receipt),
    ]
    if require_clean:
        command.append("--require-clean-worktree")
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    return result, json.loads(receipt.read_text(encoding="utf-8"))


def test_shared_policy_has_distinct_fail_closed_repository_roles() -> None:
    policy = load_policy()
    canonical = policy["canonical"]
    legacy = policy["legacy"]
    controls = policy["policy"]

    assert canonical["repository"] == "ArchonMegalon/propertyquarry"
    assert canonical["release_authority"] is True
    assert legacy["repository"] == "ArchonMegalon/property"
    assert legacy["release_authority"] is False
    assert canonical["repository"] != legacy["repository"]
    assert canonical["url"] != legacy["url"]
    assert controls == {
        "allow_legacy_release_manifest": False,
        "allow_legacy_release_workflows": False,
        "canonical_may_require_legacy_runtime": False,
        "require_distinct_repositories": True,
    }


def test_compatibility_constants_are_not_self_referential() -> None:
    assert CANONICAL_REPOSITORY == "ArchonMegalon/propertyquarry"
    assert MIRROR_REPOSITORY == "ArchonMegalon/property"
    assert CANONICAL_REPOSITORY != MIRROR_REPOSITORY
    assert CANONICAL_URL != MIRROR_URL


def test_canonical_checkout_passes_with_release_authority(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, role="canonical")
    result, receipt = _run(
        repo,
        tmp_path,
        repository=CANONICAL_REPOSITORY,
        role="canonical",
    )

    assert result.returncode == 0, result.stderr
    assert receipt["passed"] is True
    assert receipt["role"] == "canonical"
    assert receipt["release_authority"] is True
    assert receipt["self_referential"] is False


def test_legacy_checkout_passes_only_without_release_authority(
    tmp_path: Path,
) -> None:
    repo = _fixture_repo(tmp_path, role="legacy")
    result, receipt = _run(
        repo,
        tmp_path,
        repository=MIRROR_REPOSITORY,
        role="legacy",
    )

    assert result.returncode == 0, result.stderr
    assert receipt["passed"] is True
    assert receipt["role"] == "legacy"
    assert receipt["release_authority"] is False


def test_self_referential_policy_fails_before_role_proof(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, role="canonical")
    policy_path = repo / POLICY_PATH
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["legacy"]["repository"] = policy["canonical"]["repository"]
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    result, receipt = _run(
        repo,
        tmp_path,
        repository=CANONICAL_REPOSITORY,
        role="canonical",
        require_clean=False,
    )

    assert result.returncode == 1
    assert receipt["operational_errors"] == [
        "repository_role_policy_legacy_invalid"
    ]


def test_legacy_manifest_authority_marker_fails_closed(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, role="legacy")
    manifest = repo / "docs" / "PROPERTYQUARRY_RELEASE_MANIFEST.md"
    manifest.write_text(_manifest(), encoding="utf-8")
    result, receipt = _run(
        repo,
        tmp_path,
        repository=MIRROR_REPOSITORY,
        role="legacy",
        require_clean=False,
    )

    assert result.returncode == 2
    assert "legacy_release_manifest_authority_marker_present" in receipt["failures"]
    assert (
        "legacy_release_manifest_authority_end_marker_present"
        in receipt["failures"]
    )


def test_legacy_release_workflow_fails_closed(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, role="legacy")
    forbidden = repo / LEGACY_FORBIDDEN_WORKFLOWS[0]
    forbidden.write_text("name: forbidden release\n", encoding="utf-8")
    result, receipt = _run(
        repo,
        tmp_path,
        repository=MIRROR_REPOSITORY,
        role="legacy",
        require_clean=False,
    )

    assert result.returncode == 2
    assert any(
        str(item).startswith("legacy_release_workflow_present:")
        for item in receipt["failures"]
    )


def test_wrong_origin_or_role_fails_closed(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, role="canonical")
    _git(repo, "remote", "set-url", "origin", MIRROR_URL)
    result, receipt = _run(
        repo,
        tmp_path,
        repository=CANONICAL_REPOSITORY,
        role="legacy",
        require_clean=False,
    )

    assert result.returncode == 2
    assert "expected_repository_role_mismatch" in receipt["failures"]
    assert "origin_url_role_mismatch" in receipt["failures"]


def test_dirty_canonical_checkout_cannot_emit_release_role_proof(
    tmp_path: Path,
) -> None:
    repo = _fixture_repo(tmp_path, role="canonical")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    result, receipt = _run(
        repo,
        tmp_path,
        repository=CANONICAL_REPOSITORY,
        role="canonical",
    )

    assert result.returncode == 2
    assert "worktree_not_clean" in receipt["failures"]


def test_workflow_and_release_bundle_use_repository_role_not_self_mirror() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/smoke-runtime.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    role_job = jobs["propertyquarry-repository-role-contract"]
    assert role_job["permissions"] == {"contents": "read"}
    assert role_job["runs-on"] == "ubuntu-latest"
    checkout = role_job["steps"][0]
    assert checkout["with"] == {
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    initialized = role_job["steps"][1]["run"]
    assert "propertyquarry.repository_role.ci_preflight.v1" in initialized
    gate_run = role_job["steps"][2]["run"]
    assert "scripts/check_property_repository_role.py" in gate_run
    assert "--expected-repository ArchonMegalon/propertyquarry" in gate_run
    assert "--expected-role canonical" in gate_run
    assert "--expected-head-sha" in gate_run
    assert "remote.propertyquarry" not in gate_run
    assert "check_property_mirror_role.py" not in gate_run
    assert "propertyquarry-repository-role-contract" in jobs[
        "propertyquarry-ordinary-ci-success"
    ]["needs"]

    release_gate = (ROOT / "scripts/property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    assert "scripts/check_property_repository_role.py" in release_gate
    assert "--expected-role canonical" in release_gate
    assert "--require-clean-worktree" in release_gate
    assert "scripts/check_property_mirror_role.py" not in release_gate

    isolation = (ROOT / "docs/REPO_ISOLATION.md").read_text(encoding="utf-8")
    assert "`ArchonMegalon/propertyquarry` is the sole canonical" in isolation
    assert "`ArchonMegalon/property` is a legacy, noncanonical" in isolation
    assert "network_freshness_proven: false" in isolation
