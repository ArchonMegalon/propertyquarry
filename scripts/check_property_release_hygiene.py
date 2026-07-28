#!/usr/bin/env python3
from __future__ import annotations

import re
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from scripts.verify_generated_release_artifacts_clean import load_release_manifest
else:
    from verify_generated_release_artifacts_clean import load_release_manifest


ROOT = Path(__file__).resolve().parents[1]
RELEASE_GIT_COMMAND = (
    "/usr/bin/git",
    "--no-pager",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def release_git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "XDG_CONFIG_HOME": "/nonexistent",
    }

FORBIDDEN_TRACKED_PREFIXES = (
    "tmp_audit/",
)

FORBIDDEN_TRACKED_EXACT_PATHS = {
    ".env",
    ".env.local",
}

FORBIDDEN_AUDIT_SUFFIXES = (
    "_audit.py",
    "_desktop.png",
    "_mobile.png",
)

IGNORED_UNTRACKED_PREFIXES = (
    "_completion/",
    "_tmp_live_shots/",
    ".pytest_cache/",
    "build/",
    "state/",
    "tmp_audit/",
)

ALLOWED_17217_HOST_PATHS = {
    "docker-compose.yml",
}

LOCAL_API_TOKEN_MARKER = "propertyquarry-" + "local-api-token"
LOCAL_BRIDGE_HOST = ".".join(("172", "17", "0", "1"))

TEXT_SUFFIXES = {
    ".py",
    ".sh",
    ".http",
    ".md",
    ".txt",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".env",
}

BEARER_LITERAL_RE = re.compile(
    r"Authorization:\s*Bearer\s+(?!\$\{?[A-Z_][A-Z0-9_]*\}?|\{\{[^}]+\}\}|<token>|REDACTED\b)([A-Za-z0-9._-]+)",
    flags=re.IGNORECASE,
)
MANIFEST_RUNTIME_COMMIT_RE = re.compile(r"^\|\s*Runtime commit SHA\s*\|\s*`?([0-9a-f]{7,40})`?\s*\|", flags=re.MULTILINE)

RELEASE_METADATA_DESCENDANT_PATHS = {
    ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
    ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
    "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
}


def tracked_paths() -> list[str]:
    result = subprocess.run(
        [*RELEASE_GIT_COMMAND, "ls-files", "-z"],
        cwd=ROOT,
        env=release_git_environment(),
        check=True,
        capture_output=True,
        text=False,
    )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def git_head_sha() -> str:
    result = subprocess.run(
        [*RELEASE_GIT_COMMAND, "rev-parse", "HEAD"],
        cwd=ROOT,
        env=release_git_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_head_parent_sha() -> str:
    result = subprocess.run(
        [*RELEASE_GIT_COMMAND, "rev-parse", "HEAD^"],
        cwd=ROOT,
        env=release_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def git_commit_is_ancestor(commit_sha: str, head_sha: str) -> bool:
    if not commit_sha or not head_sha:
        return False
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "merge-base",
            "--is-ancestor",
            commit_sha,
            head_sha,
        ],
        cwd=ROOT,
        env=release_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def committed_paths_since(commit_sha: str, head_sha: str) -> list[str] | None:
    if not commit_sha or not head_sha:
        return None
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "log",
            "-m",
            "--format=",
            "--name-only",
            "--no-renames",
            "-z",
            f"{commit_sha}..{head_sha}",
        ],
        cwd=ROOT,
        env=release_git_environment(),
        check=False,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        return None
    return sorted(
        {
            raw_path.decode("utf-8", errors="surrogateescape")
            for raw_path in result.stdout.split(b"\0")
            if raw_path
        }
    )


def manifest_release_binding(
    manifest_sha: str,
    head_sha: str,
    parent_sha: str,
) -> tuple[bool, list[str]]:
    if (
        head_sha.startswith(manifest_sha)
        or manifest_sha.startswith(head_sha)
        or (parent_sha and parent_sha.startswith(manifest_sha))
        or (parent_sha and manifest_sha.startswith(parent_sha))
    ):
        return True, []
    if not git_commit_is_ancestor(manifest_sha, head_sha):
        return False, []
    descendant_paths = committed_paths_since(manifest_sha, head_sha)
    if descendant_paths is None:
        return False, []
    return all(path in RELEASE_METADATA_DESCENDANT_PATHS for path in descendant_paths), descendant_paths


def _git_status_rows() -> list[str]:
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        cwd=ROOT,
        env=release_git_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def release_source_state(
    root: Path,
    *,
    excluded_generated_paths: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Return fail-closed worktree state for release-source binding."""
    root = root.resolve(strict=True)
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--no-renames",
        ],
        cwd=root,
        env=release_git_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    tracked_dirty_paths: list[str] = []
    untracked_source_paths: list[str] = []
    excluded_paths: list[str] = []
    for row in result.stdout.splitlines():
        if len(row) < 4:
            continue
        status_code = row[:2]
        path = _normalize_status_path(row[3:])
        if not path:
            continue
        if path in excluded_generated_paths:
            excluded_paths.append(path)
        elif status_code == "??":
            untracked_source_paths.append(path)
        else:
            tracked_dirty_paths.append(path)
    tracked_dirty_paths.sort()
    untracked_source_paths.sort()
    excluded_paths.sort()
    return {
        "clean": not tracked_dirty_paths and not untracked_source_paths,
        "tracked_dirty_paths": tracked_dirty_paths,
        "untracked_source_paths": untracked_source_paths,
        "excluded_generated_paths": excluded_paths,
    }


def release_manifest_runtime_sha() -> str:
    manifest = ROOT / "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"
    try:
        values = load_release_manifest(manifest)
    except (OSError, ValueError):
        return ""
    return str(values.get("release_commit_sha") or "").strip()


def looks_like_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    return path.name.startswith(".env")


def _normalize_status_path(raw_path: str) -> str:
    normalized = str(raw_path or "").strip().replace("\\", "/")
    if " -> " in normalized:
        normalized = normalized.split(" -> ", 1)[1].strip()
    return normalized


def _is_release_source_path(rel_path: str) -> bool:
    normalized = str(rel_path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in IGNORED_UNTRACKED_PREFIXES):
        return False
    # Release hygiene is fail-closed: an untracked path can affect source,
    # packaging, configuration, or test collection unless it belongs to an
    # explicitly named runtime/generated-output prefix above.
    return True


def build_release_hygiene_receipt() -> dict[str, object]:
    failures: list[str] = []
    manifest_sha = release_manifest_runtime_sha()
    head_sha = git_head_sha()
    parent_sha = git_head_parent_sha()
    manifest_binding_ok = False
    manifest_descendant_paths: list[str] = []
    if not manifest_sha:
        failures.append("release manifest runtime commit missing: docs/PROPERTYQUARRY_RELEASE_MANIFEST.md")
    else:
        manifest_binding_ok, manifest_descendant_paths = manifest_release_binding(
            manifest_sha,
            head_sha,
            parent_sha,
        )
    if manifest_sha and not manifest_binding_ok:
        disallowed_descendants = [
            path for path in manifest_descendant_paths if path not in RELEASE_METADATA_DESCENDANT_PATHS
        ]
        failures.append(
            "release manifest runtime commit is not HEAD, its parent, or a metadata-only ancestor: "
            f"manifest={manifest_sha} head={head_sha} parent={parent_sha} "
            f"disallowed_descendants={disallowed_descendants}"
        )
    tracked_dirty_paths: list[str] = []
    untracked_release_source_paths: list[str] = []
    for row in _git_status_rows():
        if len(row) < 4:
            continue
        status_code = row[:2]
        path = _normalize_status_path(row[3:])
        if not path:
            continue
        if status_code == "??":
            if _is_release_source_path(path):
                untracked_release_source_paths.append(path)
            continue
        tracked_dirty_paths.append(path)
    if tracked_dirty_paths:
        preview = ", ".join(tracked_dirty_paths[:12])
        if len(tracked_dirty_paths) > 12:
            preview += f", +{len(tracked_dirty_paths) - 12} more"
        failures.append(f"tracked worktree must be clean before release: {preview}")
    if untracked_release_source_paths:
        preview = ", ".join(untracked_release_source_paths[:12])
        if len(untracked_release_source_paths) > 12:
            preview += f", +{len(untracked_release_source_paths) - 12} more"
        failures.append(f"untracked release source files forbidden before release: {preview}")
    for rel_path in tracked_paths():
        normalized = rel_path.replace("\\", "/")
        path = ROOT / normalized
        if not path.exists():
            continue
        if normalized in FORBIDDEN_TRACKED_EXACT_PATHS:
            failures.append(f"tracked live env file forbidden: {normalized}")
            continue
        if any(normalized.startswith(prefix) for prefix in FORBIDDEN_TRACKED_PREFIXES):
            failures.append(f"tracked audit scratch path forbidden: {normalized}")
            continue
        if any(normalized.endswith(suffix) for suffix in FORBIDDEN_AUDIT_SUFFIXES):
            failures.append(f"tracked audit artifact forbidden: {normalized}")
        if not path.is_file() or not looks_like_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if LOCAL_API_TOKEN_MARKER in text:
            failures.append(f"hardcoded local API token marker forbidden in tracked file: {normalized}")
        if LOCAL_BRIDGE_HOST in text and normalized not in ALLOWED_17217_HOST_PATHS:
            failures.append(f"raw {LOCAL_BRIDGE_HOST} host reference forbidden in tracked file: {normalized}")
        if BEARER_LITERAL_RE.search(text):
            failures.append(f"hardcoded bearer authorization forbidden in tracked file: {normalized}")
    required_checks = [
        "release_manifest_runtime_commit_matches_head_parent_or_metadata_only_ancestor",
        "tracked_worktree_clean",
        "no_untracked_release_source_files",
        "no_tracked_live_env_files",
        "no_tracked_audit_scratch_paths",
        "no_tracked_audit_artifacts",
        "no_hardcoded_local_api_token_marker",
        "no_raw_local_bridge_host_refs",
        "no_hardcoded_bearer_authorization",
    ]
    return {
        "schema": "propertyquarry.release_hygiene_receipt.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": "pass" if not failures else "fail",
        "required_checks": required_checks,
        "failure_count": len(failures),
        "failures": failures,
        "manifest_runtime_commit": manifest_sha,
        "head_commit": head_sha,
        "parent_commit": parent_sha,
        "manifest_descendant_paths": manifest_descendant_paths,
        "manifest_metadata_only_ancestor": bool(manifest_descendant_paths) and manifest_binding_ok,
        "tracked_dirty_path_count": len(tracked_dirty_paths),
        "untracked_release_source_count": len(untracked_release_source_paths),
        "note": "Repository hygiene and release-manifest authority gate for the tracked PropertyQuarry release plane.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check PropertyQuarry release hygiene.")
    parser.add_argument("--write", default="", help="Optional path for a JSON receipt.")
    args = parser.parse_args()

    receipt = build_release_hygiene_receipt()
    failures = list(receipt.get("failures") or [])
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if failures:
        print("property release hygiene check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("ok: property release hygiene")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
