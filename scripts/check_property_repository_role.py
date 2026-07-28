#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

try:
    from scripts.verify_generated_release_artifacts_clean import (
        RELEASE_MANIFEST_JSON_END,
        RELEASE_MANIFEST_JSON_START,
        RELEASE_MANIFEST_PATH,
        _parse_release_manifest,
        _release_manifest_shape_issues,
    )
except ModuleNotFoundError:  # Direct execution places scripts/ on sys.path.
    from verify_generated_release_artifacts_clean import (  # type: ignore[no-redef]
        RELEASE_MANIFEST_JSON_END,
        RELEASE_MANIFEST_JSON_START,
        RELEASE_MANIFEST_PATH,
        _parse_release_manifest,
        _release_manifest_shape_issues,
    )


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = Path("config/release/propertyquarry_repository_role.v1.json")
SCHEMA = "propertyquarry.repository_role.v1"
POLICY_SCHEMA = "propertyquarry.repository_role_policy.v1"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ROLE_VALUES = ("canonical", "legacy")
NO_GITHUB_ACTIONS_MARKER = Path(".github/NO_GITHUB_ACTIONS.md")
CANONICAL_LOCAL_AUTHORITY_SURFACES = (
    Path("docker-compose.property.yml"),
    Path("docker-compose.cloudflared.yml"),
    Path("scripts/deploy_propertyquarry.sh"),
    Path("scripts/propertyquarry_local_deployment_receipt.py"),
    Path("scripts/propertyquarry_release_local_container_gate.py"),
    Path("scripts/propertyquarry_release_local_runtime_gate.py"),
)


class RepositoryRoleError(RuntimeError):
    pass


def _regular_file(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RepositoryRoleError(f"required_file_unavailable:{path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RepositoryRoleError(f"required_file_not_regular:{path}")
    return path


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(_regular_file(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepositoryRoleError(f"json_invalid:{path}") from exc
    if type(payload) is not dict:
        raise RepositoryRoleError(f"json_object_required:{path}")
    return payload


def load_policy(root: Path = ROOT) -> dict[str, object]:
    policy = _load_json_object(root / POLICY_PATH)
    if set(policy) != {"schema", "product", "canonical", "legacy", "policy"}:
        raise RepositoryRoleError("repository_role_policy_shape_invalid")
    if policy.get("schema") != POLICY_SCHEMA:
        raise RepositoryRoleError("repository_role_policy_schema_invalid")
    if policy.get("product") != "PropertyQuarry":
        raise RepositoryRoleError("repository_role_policy_product_invalid")
    canonical = policy.get("canonical")
    legacy = policy.get("legacy")
    controls = policy.get("policy")
    if type(canonical) is not dict or type(legacy) is not dict:
        raise RepositoryRoleError("repository_role_policy_roles_invalid")
    if type(controls) is not dict:
        raise RepositoryRoleError("repository_role_policy_controls_invalid")
    expected_canonical = {
        "branch": "main",
        "release_authority": True,
        "repository": "ArchonMegalon/propertyquarry",
        "url": "https://github.com/ArchonMegalon/propertyquarry.git",
    }
    expected_legacy = {
        "branch": "main",
        "release_authority": False,
        "repository": "ArchonMegalon/property",
        "required_posture": "noncanonical_verifier_only",
        "url": "https://github.com/ArchonMegalon/property.git",
    }
    expected_controls = {
        "allow_legacy_release_manifest": False,
        "allow_legacy_release_workflows": False,
        "canonical_may_require_legacy_runtime": False,
        "github_actions_release_authority": False,
        "release_proof_plane": "local_docker_operator_receipts",
        "require_distinct_repositories": True,
    }
    if canonical != expected_canonical:
        raise RepositoryRoleError("repository_role_policy_canonical_invalid")
    if legacy != expected_legacy:
        raise RepositoryRoleError("repository_role_policy_legacy_invalid")
    if controls != expected_controls:
        raise RepositoryRoleError("repository_role_policy_controls_mismatch")
    if canonical["repository"] == legacy["repository"]:
        raise RepositoryRoleError("repository_role_policy_self_reference")
    if canonical["url"] == legacy["url"]:
        raise RepositoryRoleError("repository_role_policy_url_self_reference")
    return policy


def _git(root: Path, *args: str) -> str:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [
            "/usr/bin/git",
            "--no-pager",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(root),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise RepositoryRoleError(f"git_{args[0] if args else 'command'}_failed")
    return result.stdout.strip()


def _origin_url(root: Path) -> str:
    values = _git(root, "config", "--get-all", "remote.origin.url").splitlines()
    if len(values) != 1:
        raise RepositoryRoleError("origin_url_must_be_singular")
    return values[0]


def _role_for_repository(
    policy: dict[str, object],
    repository: str,
) -> str:
    for role in ROLE_VALUES:
        role_policy = policy.get(role)
        if type(role_policy) is dict and role_policy.get("repository") == repository:
            return role
    raise RepositoryRoleError("repository_not_declared_by_role_policy")


def _manifest_issues(root: Path, *, role: str) -> list[str]:
    manifest_path = root / RELEASE_MANIFEST_PATH
    try:
        manifest_text = _regular_file(manifest_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError, RepositoryRoleError) as exc:
        return [f"release_manifest_unavailable:{exc}"]
    if role == "legacy":
        issues: list[str] = []
        if RELEASE_MANIFEST_JSON_START in manifest_text:
            issues.append("legacy_release_manifest_authority_marker_present")
        if RELEASE_MANIFEST_JSON_END in manifest_text:
            issues.append("legacy_release_manifest_authority_end_marker_present")
        if "NONCANONICAL" not in manifest_text:
            issues.append("legacy_release_manifest_tombstone_missing")
        if "ArchonMegalon/propertyquarry" not in manifest_text:
            issues.append("legacy_release_manifest_canonical_pointer_missing")
        return issues
    values, issues = _parse_release_manifest(manifest_text)
    issues.extend(_release_manifest_shape_issues(values))
    if values.get("release_repository") != "ArchonMegalon/propertyquarry":
        issues.append("canonical_release_manifest_repository_mismatch")
    if (
        values.get("release_repository_origin")
        != "https://github.com/ArchonMegalon/propertyquarry.git"
    ):
        issues.append("canonical_release_manifest_origin_mismatch")
    return list(dict.fromkeys(issues))


def _workflow_issues(root: Path, *, role: str) -> list[str]:
    issues: list[str] = []
    workflows = root / ".github" / "workflows"
    if workflows.is_symlink():
        issues.append("github_actions_workflow_directory_symlink")
    elif workflows.exists():
        if not workflows.is_dir():
            issues.append("github_actions_workflow_path_not_directory")
        else:
            for entry in sorted(workflows.iterdir(), key=lambda item: item.name):
                if entry.suffix.lower() in {".yml", ".yaml"}:
                    issues.append(
                        f"github_actions_workflow_present:{entry.relative_to(root)}"
                    )
    if not (root / NO_GITHUB_ACTIONS_MARKER).is_file():
        issues.append("no_github_actions_marker_missing")
    if role == "canonical":
        issues.extend(
            f"canonical_local_authority_surface_missing:{path}"
            for path in CANONICAL_LOCAL_AUTHORITY_SURFACES
            if not (root / path).is_file()
        )
    return issues


def audit_repository_role(
    root: Path = ROOT,
    *,
    expected_repository: str = "",
    expected_role: str = "",
    expected_head_sha: str = "",
    require_clean_worktree: bool = False,
) -> dict[str, object]:
    root = root.resolve(strict=True)
    policy = load_policy(root)
    canonical = dict(policy["canonical"])  # type: ignore[arg-type]
    legacy = dict(policy["legacy"])  # type: ignore[arg-type]
    repository = str(
        expected_repository
        or os.environ.get("PROPERTYQUARRY_REPOSITORY")
        or ""
    )
    if not repository:
        origin = _origin_url(root)
        matches = [
            role_policy["repository"]
            for role_policy in (canonical, legacy)
            if role_policy["url"] == origin
        ]
        if len(matches) != 1:
            raise RepositoryRoleError("repository_identity_cannot_be_inferred")
        repository = str(matches[0])
    if REPOSITORY.fullmatch(repository) is None:
        raise RepositoryRoleError("repository_identity_malformed")
    role = _role_for_repository(policy, repository)
    failures: list[str] = []
    if expected_role and role != expected_role:
        failures.append("expected_repository_role_mismatch")
    role_policy = dict(policy[role])  # type: ignore[arg-type]
    try:
        origin_url = _origin_url(root)
    except RepositoryRoleError as exc:
        failures.append(str(exc))
        origin_url = ""
    if origin_url != role_policy["url"]:
        failures.append("origin_url_role_mismatch")
    try:
        head_sha = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    except RepositoryRoleError as exc:
        failures.append(str(exc))
        head_sha = ""
    if expected_head_sha:
        if FULL_SHA.fullmatch(expected_head_sha) is None:
            failures.append("expected_head_sha_malformed")
        elif head_sha != expected_head_sha:
            failures.append("expected_head_sha_mismatch")
    try:
        clean = not bool(
            _git(
                root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            )
        )
    except RepositoryRoleError as exc:
        failures.append(str(exc))
        clean = False
    if require_clean_worktree and not clean:
        failures.append("worktree_not_clean")
    failures.extend(_manifest_issues(root, role=role))
    failures.extend(_workflow_issues(root, role=role))
    unique_failures = list(dict.fromkeys(failures))
    return {
        "schema": SCHEMA,
        "product": "PropertyQuarry",
        "repository": repository,
        "role": role,
        "release_authority": bool(role_policy["release_authority"]),
        "canonical_repository": canonical["repository"],
        "legacy_repository": legacy["repository"],
        "self_referential": canonical["repository"] == legacy["repository"],
        "origin_url": origin_url,
        "head_sha": head_sha,
        "clean": clean,
        "policy_path": POLICY_PATH.as_posix(),
        "release_proof_plane": "local_docker_operator_receipts",
        "github_actions_release_authority": False,
        "observation_scope": "local_policy_git_config_checkout_and_docker_release_surfaces",
        "network_freshness_proven": False,
        "passed": not unique_failures,
        "failures": unique_failures,
    }


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail closed unless this checkout's canonical or legacy role "
            "matches the shared PropertyQuarry repository-role policy."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--expected-repository", default="")
    parser.add_argument("--expected-role", choices=ROLE_VALUES, default="")
    parser.add_argument("--expected-head-sha", default="")
    parser.add_argument("--require-clean-worktree", action="store_true")
    parser.add_argument("--write", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = audit_repository_role(
            args.repo_root,
            expected_repository=args.expected_repository,
            expected_role=args.expected_role,
            expected_head_sha=args.expected_head_sha,
            require_clean_worktree=args.require_clean_worktree,
        )
    except (OSError, RepositoryRoleError) as exc:
        receipt = {
            "schema": SCHEMA,
            "product": "PropertyQuarry",
            "passed": False,
            "failures": [],
            "operational_errors": [str(exc)],
        }
        if args.write:
            _write_receipt(args.write, receipt)
        print(f"property repository-role audit could not complete: {exc}", file=sys.stderr)
        return 1
    if args.write:
        _write_receipt(args.write, receipt)
    if not receipt["passed"]:
        print(
            "property repository-role audit failed: "
            + ", ".join(str(item) for item in receipt["failures"]),
            file=sys.stderr,
        )
        return 2
    authority = "enabled" if receipt["release_authority"] else "disabled"
    print(
        f"ok: {receipt['repository']} is {receipt['role']} "
        f"(release authority {authority})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
