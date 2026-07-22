#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.verify_generated_release_artifacts_clean import (
        RELEASE_MANIFEST_PATH,
        RELEASE_MANIFEST_STATIC_VALUES,
        _parse_release_manifest,
        _release_manifest_shape_issues,
    )
except ModuleNotFoundError:  # Direct execution places scripts/ on sys.path.
    from verify_generated_release_artifacts_clean import (  # type: ignore[no-redef]
        RELEASE_MANIFEST_PATH,
        RELEASE_MANIFEST_STATIC_VALUES,
        _parse_release_manifest,
        _release_manifest_shape_issues,
    )


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "propertyquarry.mirror_role.v1"
CANONICAL_REPOSITORY = "ArchonMegalon/propertyquarry"
MIRROR_REPOSITORY = "ArchonMegalon/propertyquarry"
CANONICAL_REMOTE = "origin"
MIRROR_REMOTE = "propertyquarry"
CANONICAL_URL = "https://github.com/ArchonMegalon/propertyquarry.git"
MIRROR_URL = "https://github.com/ArchonMegalon/propertyquarry.git"
DEFAULT_CANONICAL_REF = "refs/remotes/origin/main"
DEFAULT_MIRROR_REF = "refs/remotes/propertyquarry/main"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_RECEIPT_PATHS = 256

CRITICAL_EXACT_PATHS = {
    ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
    ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
    "docker-compose.property.yml",
    "docker-compose.property-legacy-edge.yml",
    "ea/Dockerfile.property",
    "ea/Dockerfile.property-web",
    "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
}
CRITICAL_PATH_PREFIXES = (
    ".github/workflows/",
    "config/",
    "ea/app/",
    "scripts/",
    "tests/",
)


class AuditOperationalError(RuntimeError):
    pass


def _git(
    repo_root: Path,
    *args: str,
    accepted_returncodes: Sequence[int] = (0,),
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode not in accepted_returncodes:
        operation = args[0] if args else "command"
        raise AuditOperationalError(f"git_{operation}_failed")
    return result


def _config_values(repo_root: Path, key: str) -> list[str]:
    result = _git(
        repo_root,
        "config",
        "--get-all",
        key,
        accepted_returncodes=(0, 1),
    )
    if result.returncode == 1:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _url_rewrite_count(repo_root: Path) -> int:
    result = _git(repo_root, "config", "--show-origin", "--name-only", "--list")
    count = 0
    for line in result.stdout.splitlines():
        key = line.rsplit("\t", 1)[-1].strip().lower()
        if key.startswith("url.") and (
            key.endswith(".insteadof") or key.endswith(".pushinsteadof")
        ):
            count += 1
    return count


def _remote_url_policy(
    repo_root: Path,
    remote: str,
    expected_url: str,
) -> dict[str, object]:
    fetch_urls = _config_values(repo_root, f"remote.{remote}.url")
    configured_push_urls = _config_values(repo_root, f"remote.{remote}.pushurl")
    effective_push_urls = configured_push_urls or fetch_urls
    return {
        "remote": remote,
        "fetch_url_count": len(fetch_urls),
        "push_url_count": len(effective_push_urls),
        "fetch_url_matches": fetch_urls == [expected_url],
        "push_url_matches": effective_push_urls == [expected_url],
    }


def _resolve_commit(repo_root: Path, ref: str) -> str:
    result = _git(
        repo_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{ref}^{{commit}}",
    )
    sha = result.stdout.strip()
    if FULL_SHA.fullmatch(sha) is None:
        raise AuditOperationalError("git_ref_resolution_returned_invalid_sha")
    return sha


def _resolve_tree(repo_root: Path, commit_sha: str) -> str:
    result = _git(repo_root, "rev-parse", "--verify", f"{commit_sha}^{{tree}}")
    tree = result.stdout.strip()
    if FULL_SHA.fullmatch(tree) is None:
        raise AuditOperationalError("git_tree_resolution_returned_invalid_oid")
    return tree


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = _git(
        repo_root,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        accepted_returncodes=(0, 1),
    )
    return result.returncode == 0


def _merge_base(repo_root: Path, left: str, right: str) -> str:
    result = _git(
        repo_root,
        "merge-base",
        left,
        right,
        accepted_returncodes=(0, 1),
    )
    if result.returncode == 1:
        return ""
    merge_base = result.stdout.strip()
    if merge_base and FULL_SHA.fullmatch(merge_base) is None:
        raise AuditOperationalError("git_merge_base_returned_invalid_sha")
    return merge_base


def _rev_count(repo_root: Path, revision_range: str) -> int:
    result = _git(repo_root, "rev-list", "--count", revision_range)
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise AuditOperationalError("git_rev_list_returned_invalid_count") from exc


def _display_path(raw_path: bytes) -> str:
    return raw_path.decode("utf-8", errors="backslashreplace")


def _changed_paths(repo_root: Path, left: str, right: str) -> list[str]:
    result = _git(
        repo_root,
        "diff",
        "--name-only",
        "-z",
        left,
        right,
        text=False,
    )
    raw_paths = [path for path in result.stdout.split(b"\0") if path]
    return [_display_path(path) for path in sorted(raw_paths)]


def _is_critical_path(path: str) -> bool:
    return path in CRITICAL_EXACT_PATHS or path.startswith(CRITICAL_PATH_PREFIXES)


def _worktree_inventory(repo_root: Path) -> dict[str, int]:
    result = _git(repo_root, "worktree", "list", "--porcelain")
    records = [record for record in result.stdout.split("\n\n") if record.strip()]
    prunable = sum(
        1
        for record in records
        if any(line.startswith("prunable") for line in record.splitlines())
    )
    return {
        "registered": len(records),
        "active": len(records) - prunable,
        "prunable": prunable,
    }


def _worktree_checkout(repo_root: Path) -> dict[str, object]:
    head_sha = _resolve_commit(repo_root, "HEAD")
    status = _git(
        repo_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        text=False,
    )
    return {
        "head_sha": head_sha,
        "clean": not bool(status.stdout),
    }


def _manifest_role_issues(repo_root: Path, commit_sha: str) -> list[str]:
    manifest_path = Path(RELEASE_MANIFEST_PATH).as_posix()
    result = _git(
        repo_root,
        "show",
        f"{commit_sha}:{manifest_path}",
        accepted_returncodes=(0, 128),
    )
    if result.returncode != 0:
        return ["release_manifest_missing"]
    values, issues = _parse_release_manifest(result.stdout)
    issues.extend(_release_manifest_shape_issues(values))
    for field, expected in RELEASE_MANIFEST_STATIC_VALUES.items():
        if values.get(field) != expected:
            issues.append(f"release_manifest_role_mismatch:{field}")
    return list(dict.fromkeys(issues))


def _topology(
    repo_root: Path,
    canonical_sha: str,
    mirror_sha: str,
) -> dict[str, object]:
    canonical_tree = _resolve_tree(repo_root, canonical_sha)
    mirror_tree = _resolve_tree(repo_root, mirror_sha)
    merge_base = _merge_base(repo_root, canonical_sha, mirror_sha)
    canonical_ahead = 0
    mirror_ahead = 0
    if canonical_sha == mirror_sha:
        classification = "exact"
        merge_base = canonical_sha
    elif not merge_base:
        classification = "history_incomplete"
    elif _is_ancestor(repo_root, mirror_sha, canonical_sha):
        classification = "mirror_lagging"
        canonical_ahead = _rev_count(repo_root, f"{mirror_sha}..{canonical_sha}")
    elif _is_ancestor(repo_root, canonical_sha, mirror_sha):
        classification = "mirror_ahead"
        mirror_ahead = _rev_count(repo_root, f"{canonical_sha}..{mirror_sha}")
    else:
        classification = "diverged"
        canonical_ahead = _rev_count(repo_root, f"{merge_base}..{canonical_sha}")
        mirror_ahead = _rev_count(repo_root, f"{merge_base}..{mirror_sha}")
    return {
        "classification": classification,
        "exact_commit": canonical_sha == mirror_sha,
        "canonical_tree_oid": canonical_tree,
        "mirror_tree_oid": mirror_tree,
        "tree_equal": canonical_tree == mirror_tree,
        "merge_base": merge_base,
        "canonical_ahead_by": canonical_ahead,
        "mirror_ahead_by": mirror_ahead,
    }


def audit_repository(
    repo_root: Path,
    *,
    canonical_ref: str = DEFAULT_CANONICAL_REF,
    mirror_ref: str = DEFAULT_MIRROR_REF,
    mirror_candidate_ref: str = "",
    expected_canonical_sha: str = "",
    expected_mirror_sha: str = "",
    expected_candidate_sha: str = "",
    require_single_worktree: bool = False,
    require_head_at_canonical: bool = False,
    require_clean_worktree: bool = False,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    failures: list[str] = []
    rewrite_count = _url_rewrite_count(repo_root)
    canonical_remote = _remote_url_policy(
        repo_root, CANONICAL_REMOTE, CANONICAL_URL
    )
    mirror_remote = _remote_url_policy(repo_root, MIRROR_REMOTE, MIRROR_URL)
    if rewrite_count:
        failures.append("git_url_rewrite_configured")
    for label, remote in (("canonical", canonical_remote), ("mirror", mirror_remote)):
        if not remote["fetch_url_matches"]:
            failures.append(f"{label}_fetch_url_mismatch")
        if not remote["push_url_matches"]:
            failures.append(f"{label}_push_url_mismatch")

    canonical_sha = _resolve_commit(repo_root, canonical_ref)
    mirror_sha = _resolve_commit(repo_root, mirror_ref)
    candidate_sha = (
        _resolve_commit(repo_root, mirror_candidate_ref)
        if mirror_candidate_ref
        else ""
    )
    if expected_canonical_sha and canonical_sha != expected_canonical_sha:
        failures.append("canonical_expected_sha_mismatch")
    if expected_mirror_sha and mirror_sha != expected_mirror_sha:
        failures.append("mirror_expected_sha_mismatch")
    if mirror_candidate_ref:
        if not expected_candidate_sha:
            failures.append("mirror_candidate_expected_sha_required")
        elif candidate_sha != expected_candidate_sha:
            failures.append("mirror_candidate_expected_sha_mismatch")
    elif expected_candidate_sha:
        failures.append("mirror_candidate_ref_required")

    main_topology = _topology(repo_root, canonical_sha, mirror_sha)
    comparison_sha = candidate_sha or mirror_sha
    topology = _topology(repo_root, canonical_sha, comparison_sha)
    if not topology["exact_commit"]:
        failures.append(f"topology_not_exact:{topology['classification']}")
    if mirror_candidate_ref and main_topology["classification"] not in {
        "exact",
        "mirror_lagging",
    }:
        failures.append(
            f"mirror_candidate_main_not_fast_forwardable:{main_topology['classification']}"
        )

    canonical_manifest_issues = _manifest_role_issues(repo_root, canonical_sha)
    mirror_manifest_issues = _manifest_role_issues(repo_root, mirror_sha)
    candidate_manifest_issues = (
        _manifest_role_issues(repo_root, candidate_sha) if candidate_sha else []
    )
    if canonical_manifest_issues:
        failures.append("canonical_release_manifest_invalid")
    if mirror_manifest_issues and not mirror_candidate_ref:
        failures.append("mirror_release_manifest_invalid")
    if candidate_manifest_issues:
        failures.append("mirror_candidate_release_manifest_invalid")

    changed_paths = _changed_paths(repo_root, canonical_sha, comparison_sha)
    critical_paths = [path for path in changed_paths if _is_critical_path(path)]
    worktrees = _worktree_inventory(repo_root)
    checkout = _worktree_checkout(repo_root)
    if require_single_worktree and (
        worktrees["registered"] != 1
        or worktrees["active"] != 1
        or worktrees["prunable"] != 0
    ):
        failures.append("worktree_inventory_not_single_clean_checkout")
    if require_head_at_canonical and checkout["head_sha"] != canonical_sha:
        failures.append("worktree_head_not_canonical")
    if require_clean_worktree and not checkout["clean"]:
        failures.append("worktree_not_clean")

    unique_failures = list(dict.fromkeys(failures))
    return {
        "schema": SCHEMA,
        "product": "PropertyQuarry",
        "canonical": {
            "repository": CANONICAL_REPOSITORY,
            "ref": canonical_ref,
            "sha": canonical_sha,
            "remote_policy": canonical_remote,
            "release_manifest_role_valid": not canonical_manifest_issues,
            "release_manifest_issue_count": len(canonical_manifest_issues),
        },
        "mirror": {
            "repository": MIRROR_REPOSITORY,
            "ref": mirror_ref,
            "sha": mirror_sha,
            "remote_policy": mirror_remote,
            "release_manifest_role_valid": not mirror_manifest_issues,
            "release_manifest_issue_count": len(mirror_manifest_issues),
        },
        "mirror_candidate": (
            {
                "ref": mirror_candidate_ref,
                "sha": candidate_sha,
                "release_manifest_role_valid": not candidate_manifest_issues,
                "release_manifest_issue_count": len(candidate_manifest_issues),
            }
            if mirror_candidate_ref
            else None
        ),
        "observation_mode": (
            "mirror_sync_pr_candidate"
            if mirror_candidate_ref
            else "exact_main"
        ),
        "topology": topology,
        "main_topology": main_topology,
        "worktrees": worktrees,
        "checkout": checkout,
        "diff": {
            "changed_path_count": len(changed_paths),
            "changed_paths": changed_paths[:MAX_RECEIPT_PATHS],
            "changed_paths_truncated": len(changed_paths) > MAX_RECEIPT_PATHS,
            "critical_path_count": len(critical_paths),
            "critical_paths": critical_paths[:MAX_RECEIPT_PATHS],
            "critical_paths_truncated": len(critical_paths) > MAX_RECEIPT_PATHS,
        },
        "policy": {
            "canonical_branch": "main",
            "expected_canonical_sha": expected_canonical_sha,
            "expected_mirror_sha": expected_mirror_sha,
            "expected_candidate_sha": expected_candidate_sha,
            "require_exact_commit_identity": True,
            "require_single_worktree": require_single_worktree,
            "require_head_at_canonical": require_head_at_canonical,
            "require_clean_worktree": require_clean_worktree,
            "git_url_rewrite_count": rewrite_count,
        },
        "observation_scope": "local_git_config_objects_and_refs",
        "fetch_performed_by_gate": False,
        "network_freshness_proven": False,
        "passed": not unique_failures,
        "failures": unique_failures,
    }


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
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
            "Fail closed unless PropertyQuarry's public mirror exactly matches its "
            "canonical main commit, or an explicit mirror-sync PR candidate proves "
            "the exact safe fast-forward commit."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--canonical-ref", default=DEFAULT_CANONICAL_REF)
    parser.add_argument("--mirror-ref", default=DEFAULT_MIRROR_REF)
    parser.add_argument("--mirror-candidate-ref", default="")
    parser.add_argument("--expected-canonical-sha", default="")
    parser.add_argument("--expected-mirror-sha", default="")
    parser.add_argument("--expected-candidate-sha", default="")
    parser.add_argument("--require-single-worktree", action="store_true")
    parser.add_argument("--require-head-at-canonical", action="store_true")
    parser.add_argument("--require-clean-worktree", action="store_true")
    parser.add_argument("--write", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for label, value in (
        ("expected canonical SHA", args.expected_canonical_sha),
        ("expected mirror SHA", args.expected_mirror_sha),
        ("expected candidate SHA", args.expected_candidate_sha),
    ):
        if value and FULL_SHA.fullmatch(value) is None:
            print(f"error: {label} must be 40 lowercase hexadecimal characters", file=sys.stderr)
            return 1
    try:
        receipt = audit_repository(
            args.repo_root,
            canonical_ref=args.canonical_ref,
            mirror_ref=args.mirror_ref,
            mirror_candidate_ref=args.mirror_candidate_ref,
            expected_canonical_sha=args.expected_canonical_sha,
            expected_mirror_sha=args.expected_mirror_sha,
            expected_candidate_sha=args.expected_candidate_sha,
            require_single_worktree=args.require_single_worktree,
            require_head_at_canonical=args.require_head_at_canonical,
            require_clean_worktree=args.require_clean_worktree,
        )
    except AuditOperationalError as exc:
        receipt = {
            "schema": SCHEMA,
            "product": "PropertyQuarry",
            "observation_scope": "local_git_config_objects_and_refs",
            "fetch_performed_by_gate": False,
            "network_freshness_proven": False,
            "passed": False,
            "failures": [],
            "operational_errors": [str(exc)],
        }
        if args.write:
            _write_receipt(args.write, receipt)
        print(f"property mirror-role audit could not complete: {exc}", file=sys.stderr)
        return 1

    if args.write:
        _write_receipt(args.write, receipt)
    topology = receipt["topology"]
    assert isinstance(topology, dict)
    if not receipt["passed"]:
        print(
            "property mirror-role audit failed "
            f"({topology['classification']}; "
            f"canonical ahead {topology['canonical_ahead_by']}, "
            f"mirror ahead {topology['mirror_ahead_by']})",
            file=sys.stderr,
        )
        for failure in receipt["failures"]:
            print(f"- {failure}", file=sys.stderr)
        return 2
    if receipt.get("observation_mode") == "mirror_sync_pr_candidate":
        print("ok: mirror-sync PR candidate is the exact canonical fast-forward commit")
    else:
        print("ok: property canonical and propertyquarry mirror are the same commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
