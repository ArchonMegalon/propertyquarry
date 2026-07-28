#!/usr/bin/env python3
"""Render one fail-closed PropertyQuarry operator truth view.

The report combines only checked-in candidate evidence and local Git state. It
never treats either as current GitHub Actions, deployment, or public-edge proof.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
ROLE_POLICY_PATH = Path(
    "config/release/propertyquarry_repository_role.v1.json"
)
RELEASE_MANIFEST_PATH = Path("docs/PROPERTYQUARRY_RELEASE_MANIFEST.md")
BROWSER_PROOF_PATH = Path(
    ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json"
)
MANIFEST_START = "<!-- propertyquarry-release-manifest-json:start -->"
MANIFEST_END = "<!-- propertyquarry-release-manifest-json:end -->"
SCHEMA = "propertyquarry.launch_room.v1"


class LaunchRoomError(RuntimeError):
    pass


def _object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LaunchRoomError(f"invalid_json:{path}") from exc
    if not isinstance(payload, dict):
        raise LaunchRoomError(f"json_object_required:{path}")
    return payload


def _manifest_values(root: Path) -> dict[str, object]:
    try:
        text = (root / RELEASE_MANIFEST_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LaunchRoomError("release_manifest_unreadable") from exc
    if text.count(MANIFEST_START) != 1 or text.count(MANIFEST_END) != 1:
        raise LaunchRoomError("release_manifest_authority_markers_invalid")
    marked = text.split(MANIFEST_START, 1)[1].split(MANIFEST_END, 1)[0]
    match = re.search(r"```json\s*(\{.*\})\s*```", marked, flags=re.DOTALL)
    if not match:
        raise LaunchRoomError("release_manifest_authority_json_missing")
    try:
        values = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise LaunchRoomError("release_manifest_authority_json_invalid") from exc
    if not isinstance(values, dict):
        raise LaunchRoomError("release_manifest_authority_object_required")
    return values


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _proof_counts(proof: dict[str, object]) -> dict[str, object]:
    source = (
        dict(proof.get("source_backed_journey_proof") or {})
        if isinstance(proof.get("source_backed_journey_proof"), dict)
        else {}
    )
    browser = (
        dict(proof.get("real_browser_e2e_proof") or {})
        if isinstance(proof.get("real_browser_e2e_proof"), dict)
        else {}
    )
    matrix = (
        dict(proof.get("journey_evidence_matrix") or {})
        if isinstance(proof.get("journey_evidence_matrix"), dict)
        else {}
    )
    journeys = list(matrix.get("required_journey_ids") or [])
    return {
        "source": {
            "passed": source.get("status") == "pass",
            "selected": int(source.get("selected_count") or 0),
            "required": int(source.get("required_case_count") or 0),
        },
        "browser": {
            "passed": browser.get("status") == "pass",
            "selected": int(browser.get("selected_count") or 0),
            "required": int(browser.get("required_case_count") or 0),
        },
        "journeys": {
            "passed": matrix.get("status") == "pass",
            "selected": len(journeys),
            "required": len(journeys),
        },
        "runtime_commit_sha": str(matrix.get("runtime_commit_sha") or ""),
    }


def build_launch_room(root: Path = ROOT) -> dict[str, object]:
    root = root.resolve(strict=True)
    policy = _object(root / ROLE_POLICY_PATH)
    canonical = dict(policy.get("canonical") or {})
    legacy = dict(policy.get("legacy") or {})
    if (
        canonical.get("repository") != "ArchonMegalon/propertyquarry"
        or canonical.get("release_authority") is not True
        or legacy.get("repository") != "ArchonMegalon/property"
        or legacy.get("release_authority") is not False
        or canonical.get("repository") == legacy.get("repository")
    ):
        raise LaunchRoomError("repository_role_policy_not_fail_closed")
    manifest = _manifest_values(root)
    if manifest.get("release_repository") != canonical.get("repository"):
        raise LaunchRoomError("manifest_repository_not_canonical")
    runtime_sha = str(manifest.get("release_commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", runtime_sha):
        raise LaunchRoomError("manifest_runtime_sha_invalid")
    proof = _object(root / BROWSER_PROOF_PATH)
    proof_counts = _proof_counts(proof)
    proof_runtime_sha = str(proof_counts.get("runtime_commit_sha") or "")
    candidate_bound = proof_runtime_sha == runtime_sha
    source = dict(proof_counts["source"])
    browser = dict(proof_counts["browser"])
    journeys = dict(proof_counts["journeys"])
    candidate_proof_green = (
        candidate_bound
        and all(
            row.get("passed") is True
            and row.get("selected") == row.get("required")
            and int(row.get("required") or 0) > 0
            for row in (source, browser, journeys)
        )
    )
    head_sha = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    dirty = bool(
        _git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
    )
    legacy_sha = _git(
        root,
        "rev-parse",
        "--verify",
        "refs/remotes/legacy-property/main^{commit}",
    )
    return {
        "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "product": "PropertyQuarry",
        "canonical_repository": canonical.get("repository"),
        "canonical_release_authority": True,
        "legacy_repository": legacy.get("repository"),
        "legacy_release_authority": False,
        "legacy_posture": legacy.get("required_posture"),
        "repositories_are_exact_mirrors": False,
        "envelope_head": head_sha,
        "worktree_clean": not dirty,
        "runtime_candidate": runtime_sha,
        "legacy_observed_head": legacy_sha,
        "candidate_proof": {
            "status": "pass" if candidate_proof_green else "blocked",
            "candidate_bound": candidate_bound,
            **proof_counts,
        },
        "github_actions": {
            "status": "missing_current_protected_actions_evidence",
            "network_freshness_proven": False,
        },
        "core_gold": {
            "status": (
                "candidate_eligible_production_blocked"
                if candidate_proof_green
                else "blocked_candidate_proof"
            ),
            "production_claim": False,
        },
        "advanced_visual_gold": {
            "status": "unavailable_unbound_producer_receipts",
            "production_claim": False,
        },
        "live_deployment": {
            "status": "missing_exact_candidate_protected_receipt",
            "production_launch": False,
        },
        "public_edge": {
            "status": "historical_only_current_proof_missing",
            "network_freshness_proven": False,
        },
        "next_action": (
            (
                "Commit and reseal the canonical candidate, then dispatch "
                "smoke-runtime on ArchonMegalon/propertyquarry main"
            )
            if dirty
            else (
                "Dispatch smoke-runtime on ArchonMegalon/propertyquarry main"
            )
        )
        + (
            " with the one-time release/security runner labels and "
            "release-runner ticket; preserve ordinary-CI, "
            "bootstrap-attestation, image, live, rollback, and DR receipts."
        ),
        "production_launch_ready": False,
    }


def render_markdown(report: dict[str, object]) -> str:
    proof = dict(report["candidate_proof"])
    source = dict(proof["source"])
    browser = dict(proof["browser"])
    journeys = dict(proof["journeys"])
    rows = (
        ("Canonical repo", report["canonical_repository"]),
        ("Envelope HEAD", report["envelope_head"] or "unavailable"),
        ("Runtime candidate", report["runtime_candidate"]),
        ("Legacy repo", f"{report['legacy_repository']} (noncanonical)"),
        ("Candidate proof", proof["status"]),
        (
            "Evidence counts",
            f"{source['selected']}/{source['required']} source; "
            f"{journeys['selected']}/{journeys['required']} journeys; "
            f"{browser['selected']}/{browser['required']} browser",
        ),
        ("GitHub Actions", dict(report["github_actions"])["status"]),
        ("Core Gold", dict(report["core_gold"])["status"]),
        (
            "Advanced Visual Gold",
            dict(report["advanced_visual_gold"])["status"],
        ),
        ("Live deployment", dict(report["live_deployment"])["status"]),
        ("Public edge", dict(report["public_edge"])["status"]),
        ("Production launch", "BLOCKED"),
    )
    table = "\n".join(f"| {key} | `{value}` |" for key, value in rows)
    return (
        "# PropertyQuarry launch room\n\n"
        f"Observed `{report['observed_at']}`. This view is local and does not "
        "claim network freshness.\n\n"
        "| Field | Current truth |\n"
        "| --- | --- |\n"
        f"{table}\n\n"
        "## Next action\n\n"
        f"{report['next_action']}\n"
    )


def _write(path: Path, payload: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the fail-closed PropertyQuarry launch-room truth."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--write", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_launch_room(args.root)
    except (OSError, LaunchRoomError) as exc:
        print(f"launch-room audit failed: {exc}", file=sys.stderr)
        return 2
    rendered = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_markdown(report)
    )
    if args.write:
        _write(args.write, rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
