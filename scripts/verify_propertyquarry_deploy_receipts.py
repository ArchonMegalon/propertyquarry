#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from . import propertyquarry_release_proof_baseline as release_proof_baseline
    from .materialize_ea_flagship_release_gate import browser_receipt_pass_blockers
    from .propertyquarry_release_receipt_binding import (
        CANONICAL_BROWSER_RECEIPT,
        CANONICAL_FLAGSHIP_RECEIPT,
        CANONICAL_RELEASE_MANIFEST,
        CANONICAL_SEED,
        CANONICAL_WEEKLY_PULSE,
        ReleaseBindingError,
        build_source_binding,
        canonical_regular_file,
        changed_paths,
        commit_file_bytes,
        commit_file_oid,
        commit_parents,
        commit_timestamp,
        file_digest_binding,
        git_text,
        resolve_commit,
        sha256_bytes,
    )
    from .verify_generated_release_artifacts_clean import load_release_manifest
else:
    import propertyquarry_release_proof_baseline as release_proof_baseline
    from materialize_ea_flagship_release_gate import browser_receipt_pass_blockers
    from propertyquarry_release_receipt_binding import (
        CANONICAL_BROWSER_RECEIPT,
        CANONICAL_FLAGSHIP_RECEIPT,
        CANONICAL_RELEASE_MANIFEST,
        CANONICAL_SEED,
        CANONICAL_WEEKLY_PULSE,
        ReleaseBindingError,
        build_source_binding,
        canonical_regular_file,
        changed_paths,
        commit_file_bytes,
        commit_file_oid,
        commit_parents,
        commit_timestamp,
        file_digest_binding,
        git_text,
        resolve_commit,
        sha256_bytes,
    )
    from verify_generated_release_artifacts_clean import load_release_manifest


REQUIRED_RELEASE_DOCS = (
    "README.md",
    "RUNBOOK.md",
    "RELEASE_CHECKLIST.md",
    "PRODUCT_RELEASE_CHECKLIST.md",
    "docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md",
)
MAX_RECEIPT_AGE_SECONDS = 86_400
MAX_FUTURE_SKEW_SECONDS = 300


def _load_canonical_json(root: Path, relative_path: Path, *, label: str, issues: list[str]) -> dict[str, Any]:
    try:
        path = canonical_regular_file(root, relative_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ReleaseBindingError) as exc:
        issues.append(f"{label} is not a canonical regular JSON file: {relative_path}: {exc}")
        return {}
    if not isinstance(payload, dict) or not payload:
        issues.append(f"{label} must contain a non-empty JSON object: {relative_path}")
        return {}
    return dict(payload)


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _evidence_nodes(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    nodes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        nodes.append(
            {
                "file": str(item.get("file") or "").strip(),
                "cases": _strings(item.get("cases")),
            }
        )
    return nodes


def _present_evidence_nodes(value: object) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, list):
        return [], False
    nodes: list[dict[str, Any]] = []
    all_present = True
    for item in value:
        if not isinstance(item, dict):
            return [], False
        nodes.append(
            {
                "file": str(item.get("file") or "").strip(),
                "cases": _strings(item.get("cases")),
            }
        )
        all_present = all_present and item.get("present") is True
    return nodes, all_present


def _present_path_nodes(value: object) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    paths: list[str] = []
    all_present = True
    for item in value:
        if not isinstance(item, dict):
            return [], False
        paths.append(str(item.get("path") or "").strip())
        all_present = all_present and item.get("present") is True
    return paths, all_present


def _require_empty_list(payload: dict[str, Any], key: str, *, label: str, issues: list[str]) -> None:
    value = payload.get(key)
    if not isinstance(value, list):
        issues.append(f"{label} lacks an explicit {key} list")
        return
    populated = _strings(value)
    if populated:
        issues.append(f"{label} still reports {key}: " + "; ".join(populated))


def _parse_receipt_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _verify_freshness(
    *,
    browser: dict[str, Any],
    flagship: dict[str, Any],
    pulse: dict[str, Any],
    receipt_commit_time: int,
    deploy_metadata_commit_time: int,
    max_age_seconds: int,
    now: datetime,
    issues: list[str],
) -> None:
    now_epoch = int(now.timestamp())
    if max_age_seconds < 1 or max_age_seconds > MAX_RECEIPT_AGE_SECONDS:
        issues.append(
            f"receipt max age must be between 1 and {MAX_RECEIPT_AGE_SECONDS} seconds"
        )
        return
    for label, timestamp in (
        ("receipt metadata commit", receipt_commit_time),
        ("deploy metadata commit", deploy_metadata_commit_time),
    ):
        if timestamp > now_epoch + MAX_FUTURE_SKEW_SECONDS:
            issues.append(f"{label} timestamp is unreasonably in the future")
        elif now_epoch - timestamp > max_age_seconds:
            issues.append(f"{label} is stale")
    for label, payload in (
        ("browser workflow proof", browser),
        ("flagship release receipt", flagship),
        ("weekly product pulse", pulse),
    ):
        containing_commit_time = (
            deploy_metadata_commit_time if label == "weekly product pulse" else receipt_commit_time
        )
        generated_at = _parse_receipt_time(payload.get("generated_at"))
        if generated_at is None:
            issues.append(f"{label} has a missing or invalid generated_at timestamp")
            continue
        generated_epoch = int(generated_at.timestamp())
        if generated_epoch > now_epoch + MAX_FUTURE_SKEW_SECONDS:
            issues.append(f"{label} generated_at is unreasonably in the future")
        elif now_epoch - generated_epoch > max_age_seconds:
            issues.append(f"{label} is stale")
        if generated_epoch > containing_commit_time + MAX_FUTURE_SKEW_SECONDS:
            issues.append(f"{label} was generated after its containing metadata commit")


def _verify_git_envelope(
    *,
    root: Path,
    expected_head: str,
    expected_receipt_commit: str,
    expected_code_parent: str,
    issues: list[str],
) -> tuple[str, str, str, int, int] | None:
    try:
        current_head = resolve_commit(root, "HEAD")
        head = resolve_commit(root, expected_head)
        receipt_commit = resolve_commit(root, expected_receipt_commit)
        code_parent = resolve_commit(root, expected_code_parent)
        head_parents = commit_parents(root, head)
        receipt_parents = commit_parents(root, receipt_commit)
        deploy_metadata_paths = changed_paths(root, receipt_commit, head)
        receipt_paths = changed_paths(root, code_parent, receipt_commit)
        deploy_metadata_time = commit_timestamp(root, head)
        receipt_time = commit_timestamp(root, receipt_commit)
        worktree_status = git_text(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
    except ReleaseBindingError as exc:
        issues.append(f"receipt Git envelope is invalid: {exc}")
        return None
    if expected_head.lower() != head or current_head != head:
        issues.append("expected deploy HEAD is not the repository's exact current full commit")
    if expected_receipt_commit.lower() != receipt_commit:
        issues.append("expected receipt commit is not an exact full commit")
    if expected_code_parent.lower() != code_parent:
        issues.append("expected code parent is not an exact full commit")
    if worktree_status:
        issues.append("receipt metadata commit worktree is not clean")
    if head_parents != [receipt_commit]:
        issues.append("deploy HEAD must be a single-parent manifest/pulse metadata commit directly above receipts")
    if receipt_parents != [code_parent]:
        issues.append("receipt commit must be a single-parent metadata commit directly above the code commit")
    required_receipt_changes = sorted(
        (CANONICAL_BROWSER_RECEIPT.as_posix(), CANONICAL_FLAGSHIP_RECEIPT.as_posix())
    )
    if sorted(receipt_paths) != required_receipt_changes:
        issues.append(
            "receipt metadata commit must change exactly the canonical browser and flagship receipts"
        )
    required_deploy_metadata_changes = sorted(
        (CANONICAL_RELEASE_MANIFEST.as_posix(), CANONICAL_WEEKLY_PULSE.as_posix())
    )
    if sorted(deploy_metadata_paths) != required_deploy_metadata_changes:
        issues.append(
            "deploy metadata commit must change exactly the canonical release manifest and weekly pulse"
        )
    for relative_path in (CANONICAL_BROWSER_RECEIPT, CANONICAL_FLAGSHIP_RECEIPT):
        try:
            working_path = canonical_regular_file(root, relative_path)
            working_bytes = working_path.read_bytes()
            committed_bytes = commit_file_bytes(root, head, relative_path)
            committed_oid = commit_file_oid(root, head, relative_path)
            receipt_commit_bytes = commit_file_bytes(root, receipt_commit, relative_path)
            receipt_commit_oid = commit_file_oid(root, receipt_commit, relative_path)
            expected_oid = file_digest_binding(root, relative_path)["git_blob_oid"]
        except (OSError, ReleaseBindingError) as exc:
            issues.append(f"canonical receipt tree binding failed for {relative_path}: {exc}")
            continue
        if (
            committed_bytes != working_bytes
            or receipt_commit_bytes != working_bytes
            or committed_oid != expected_oid
            or receipt_commit_oid != expected_oid
        ):
            issues.append(f"canonical receipt bytes do not match deploy HEAD: {relative_path}")
    for relative_path in (CANONICAL_RELEASE_MANIFEST, CANONICAL_WEEKLY_PULSE):
        try:
            working_path = canonical_regular_file(root, relative_path)
            working_bytes = working_path.read_bytes()
            committed_bytes = commit_file_bytes(root, head, relative_path)
            committed_oid = commit_file_oid(root, head, relative_path)
            expected_oid = file_digest_binding(root, relative_path)["git_blob_oid"]
        except (OSError, ReleaseBindingError) as exc:
            issues.append(f"canonical deploy metadata tree binding failed for {relative_path}: {exc}")
            continue
        if committed_bytes != working_bytes or committed_oid != expected_oid:
            issues.append(f"canonical deploy metadata bytes do not match deploy HEAD: {relative_path}")
    return head, receipt_commit, code_parent, receipt_time, deploy_metadata_time


def verify_deploy_receipts(
    *,
    root: Path,
    expected_head: str,
    expected_receipt_commit: str,
    expected_code_parent: str,
    max_age_seconds: int = MAX_RECEIPT_AGE_SECONDS,
    now: datetime | None = None,
) -> list[str]:
    issues: list[str] = []
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        return [f"PropertyQuarry repository root is invalid: {exc}"]

    seed = _load_canonical_json(root, CANONICAL_SEED, label="flagship gate seed", issues=issues)
    browser = _load_canonical_json(
        root,
        CANONICAL_BROWSER_RECEIPT,
        label="browser workflow proof",
        issues=issues,
    )
    flagship = _load_canonical_json(
        root,
        CANONICAL_FLAGSHIP_RECEIPT,
        label="flagship release receipt",
        issues=issues,
    )
    pulse = _load_canonical_json(
        root,
        CANONICAL_WEEKLY_PULSE,
        label="weekly product pulse",
        issues=issues,
    )
    try:
        release_manifest = load_release_manifest(
            canonical_regular_file(root, CANONICAL_RELEASE_MANIFEST)
        )
    except (OSError, UnicodeError, ValueError, ReleaseBindingError) as exc:
        issues.append(f"release manifest canonical authority is invalid: {exc}")
        release_manifest = {}
    git_envelope = _verify_git_envelope(
        root=root,
        expected_head=expected_head,
        expected_receipt_commit=expected_receipt_commit,
        expected_code_parent=expected_code_parent,
        issues=issues,
    )
    if not seed or not browser or not flagship or not pulse or not release_manifest or git_envelope is None:
        return list(dict.fromkeys(issues))
    head, receipt_commit, code_parent, receipt_time, deploy_metadata_time = git_envelope

    expected_product = str(seed.get("product") or "").strip()
    expected_surface = str(seed.get("surface") or "").strip()
    expected_version = _integer(seed.get("version"))
    proof_contract = seed.get("browser_workflow_proof")
    if not isinstance(proof_contract, dict):
        proof_contract = {}
    release_claim = seed.get("release_claim")
    if not isinstance(release_claim, dict):
        release_claim = {}
    expected_target = str(proof_contract.get("proof_target") or "").strip()
    expected_sources = _evidence_nodes(proof_contract.get("evidence_sources"))
    issues.extend(
        f"flagship gate seed release-proof baseline mismatch: {reason}"
        for reason in release_proof_baseline.approved_seed_baseline_blockers(seed)
    )

    if expected_product != "propertyquarry":
        issues.append(
            f"flagship gate seed product is {expected_product or 'missing'}, expected standalone propertyquarry"
        )
    if expected_surface != "propertyquarry_flagship_release_control":
        issues.append(
            "flagship gate seed surface is "
            f"{expected_surface or 'missing'}, expected propertyquarry_flagship_release_control"
        )
    if expected_target != "propertyquarry":
        issues.append(
            f"flagship gate seed proof target is {expected_target or 'missing'}, expected propertyquarry"
        )
    if expected_version is None or expected_version < 1:
        issues.append("flagship gate seed lacks a valid positive version")
    expected_source_files = [str(node["file"]) for node in expected_sources]
    source_backed_nodes = [node for node in expected_sources if "/e2e/" not in str(node["file"])]
    real_browser_nodes = [node for node in expected_sources if "/e2e/" in str(node["file"])]
    if (
        not expected_sources
        or any(not node["file"] or not node["cases"] for node in expected_sources)
        or len(expected_source_files) != len(set(expected_source_files))
        or not source_backed_nodes
        or len(real_browser_nodes) != 1
    ):
        issues.append(
            "flagship gate seed must define complete, unique browser evidence nodes with at least one "
            "source-backed lane and exactly one real-browser lane"
        )

    try:
        expected_source_binding = build_source_binding(
            root,
            seed_path=CANONICAL_SEED,
            evidence_sources=proof_contract.get("evidence_sources"),
            code_commit=code_parent,
        )
        expected_browser_binding = file_digest_binding(root, CANONICAL_BROWSER_RECEIPT)
    except (OSError, ReleaseBindingError) as exc:
        issues.append(f"current seed/source cryptographic binding failed: {exc}")
        expected_source_binding = None
        expected_browser_binding = None
    if (
        isinstance(expected_source_binding, dict)
        and str(expected_source_binding.get("code_commit") or "").lower() != code_parent
    ):
        issues.append(
            "expected code parent is not the exact code parent bound by the receipt source closure"
        )
    if browser.get("source_binding") != expected_source_binding:
        issues.append("browser workflow proof is not bound to the exact code parent, seed, and test sources")
    if flagship.get("source_binding") != expected_source_binding:
        issues.append("flagship release receipt is not bound to the exact code parent, seed, and test sources")
    if flagship.get("browser_receipt_binding") != expected_browser_binding:
        issues.append("flagship release receipt is not cryptographically bound to the canonical browser receipt")
    if isinstance(expected_browser_binding, dict):
        try:
            head_browser_oid = commit_file_oid(root, head, CANONICAL_BROWSER_RECEIPT)
            head_browser_bytes = commit_file_bytes(root, head, CANONICAL_BROWSER_RECEIPT)
        except ReleaseBindingError as exc:
            issues.append(f"canonical browser receipt is missing from deploy HEAD: {exc}")
        else:
            if expected_browser_binding.get("git_blob_oid") != head_browser_oid:
                issues.append("browser receipt Git blob binding does not match deploy HEAD")
            if expected_browser_binding.get("sha256") != sha256_bytes(head_browser_bytes):
                issues.append("browser receipt SHA-256 binding does not match deploy HEAD")

    manifest_runtime_commit = str(release_manifest.get("release_commit_sha") or "").lower()
    if manifest_runtime_commit != code_parent:
        issues.append(
            "release manifest Runtime commit SHA must equal the exact source/code commit"
        )
    if str(pulse.get("contract_name") or "").strip() != "ea.weekly_product_pulse":
        issues.append("weekly product pulse has the wrong contract")
    if str(pulse.get("release_truth_source") or "").strip() != CANONICAL_FLAGSHIP_RECEIPT.as_posix():
        issues.append("weekly product pulse does not name the canonical flagship receipt")
    pulse_provenance = pulse.get("release_truth_provenance")
    if not isinstance(pulse_provenance, dict):
        pulse_provenance = {}
    if str(pulse_provenance.get("repo_relative_path") or "").strip() != CANONICAL_FLAGSHIP_RECEIPT.as_posix():
        issues.append("weekly product pulse provenance does not name the canonical flagship receipt")
    if str(pulse_provenance.get("git_head") or "").strip().lower() != receipt_commit:
        issues.append("weekly product pulse provenance is not bound to the canonical receipt commit")
    try:
        flagship_bytes = canonical_regular_file(root, CANONICAL_FLAGSHIP_RECEIPT).read_bytes()
    except (OSError, ReleaseBindingError) as exc:
        issues.append(f"canonical flagship receipt digest failed: {exc}")
    else:
        if str(pulse_provenance.get("sha256") or "").strip().lower() != sha256_bytes(flagship_bytes):
            issues.append("weekly product pulse flagship SHA-256 does not match the canonical receipt")
    pulse_signals = pulse.get("supporting_signals")
    if not isinstance(pulse_signals, dict):
        pulse_signals = {}
    if str(pulse_signals.get("flagship_release_receipt_git_head") or "").strip().lower() != receipt_commit:
        issues.append("weekly product pulse supporting signal is not bound to the canonical receipt commit")

    browser_status = str(browser.get("status") or "").strip().lower()
    if browser_status != "pass":
        issues.append(f"browser workflow proof is {browser_status or 'missing'}, expected pass")
    _require_empty_list(browser, "blocking_reasons", label="browser workflow proof", issues=issues)
    _require_empty_list(browser, "current_limitations", label="browser workflow proof", issues=issues)
    try:
        browser_contract_blockers = browser_receipt_pass_blockers(browser, seed)
    except (TypeError, ValueError):
        browser_contract_blockers = ["published proof contains invalid execution counts"]
    issues.extend(
        f"browser workflow proof does not match the current seed: {reason}"
        for reason in browser_contract_blockers
    )
    if str(browser.get("seed_source") or "").strip() != CANONICAL_SEED.as_posix():
        issues.append("browser workflow proof does not name the canonical gate seed")

    flagship_status = str(flagship.get("status") or "").strip().lower()
    if flagship_status != "pass":
        issues.append(f"flagship release receipt is {flagship_status or 'missing'}, expected pass")
    _require_empty_list(flagship, "blocking_reasons", label="flagship release receipt", issues=issues)
    _require_empty_list(flagship, "current_limitations", label="flagship release receipt", issues=issues)
    if str(flagship.get("product") or "").strip() != expected_product:
        issues.append("flagship release receipt product does not match the current gate seed")
    if str(flagship.get("surface") or "").strip() != expected_surface:
        issues.append("flagship release receipt surface does not match the current gate seed")
    if _integer(flagship.get("version")) != expected_version:
        issues.append("flagship release receipt version does not match the current gate seed")
    if str(flagship.get("kind") or "").strip() != "release_receipt":
        issues.append("flagship release receipt has the wrong receipt kind")
    if str(flagship.get("generated_by") or "").strip() != "scripts/materialize_ea_flagship_release_gate.py":
        issues.append("flagship release receipt was not produced by the governed materializer")
    if flagship.get("approved_baseline") != release_proof_baseline.approved_baseline_binding():
        issues.append("flagship release receipt is not bound to the immutable approved release-proof baseline")
    if flagship.get("release_claim") != seed.get("release_claim"):
        issues.append("flagship release claim does not match the current gate seed")
    if flagship.get("journey_evidence_matrix") != browser.get("journey_evidence_matrix"):
        issues.append(
            "flagship release receipt journey evidence matrix does not exactly match the governed browser proof"
        )

    truth_seed = seed.get("truth_plane")
    if not isinstance(truth_seed, dict):
        truth_seed = {}
    truth_receipt = flagship.get("truth_plane")
    if not isinstance(truth_receipt, dict):
        truth_receipt = {}
    if truth_receipt.get("present") is not True:
        issues.append("flagship truth plane is not reported present")
    if str(truth_receipt.get("source") or "").strip() != str(truth_seed.get("source") or "").strip():
        issues.append("flagship truth-plane source does not match the current gate seed")

    receipt_proof = flagship.get("browser_workflow_proof")
    if not isinstance(receipt_proof, dict):
        receipt_proof = {}
    if str(receipt_proof.get("proof_target") or "").strip() != expected_target:
        issues.append("flagship release receipt proof target does not match the current gate seed")
    if _evidence_nodes(receipt_proof.get("evidence_sources")) != expected_sources:
        issues.append("flagship release receipt evidence nodes do not exactly match the current gate seed")
    present_sources, all_sources_present = _present_evidence_nodes(receipt_proof.get("source_files_present"))
    if present_sources != expected_sources or not all_sources_present:
        issues.append("flagship release receipt lacks the exact present source nodes required by the current seed")
    if receipt_proof.get("published_receipt_present") is not True:
        issues.append("flagship release receipt is not bound to a published browser proof")
    if str(receipt_proof.get("published_receipt") or "").strip() != CANONICAL_BROWSER_RECEIPT.as_posix():
        issues.append("flagship release receipt does not name the canonical browser proof")

    canon_seed = seed.get("ea_product_canon")
    if not isinstance(canon_seed, dict):
        canon_seed = {}
    canon_receipt = flagship.get("ea_product_canon")
    if not isinstance(canon_receipt, dict):
        canon_receipt = {}
    expected_canon_docs = _strings(canon_seed.get("required_docs"))
    if not expected_canon_docs:
        issues.append("flagship gate seed lacks required product-canon nodes")
    if str(canon_receipt.get("source_root") or "").strip() != str(canon_seed.get("source_root") or "").strip():
        issues.append("flagship product-canon source root does not match the current gate seed")
    if str(canon_receipt.get("scope_label") or "").strip() != str(canon_seed.get("scope_label") or "").strip():
        issues.append("flagship product-canon scope does not match the current gate seed")
    if _strings(canon_receipt.get("required_docs")) != expected_canon_docs:
        issues.append("flagship product-canon nodes do not exactly match the current gate seed")
    present_canon_docs, all_canon_docs_present = _present_path_nodes(canon_receipt.get("docs_present"))
    if present_canon_docs != expected_canon_docs or not all_canon_docs_present:
        issues.append("flagship release receipt lacks the exact present product-canon nodes")
    if canon_receipt.get("all_required_docs_present") is not True:
        issues.append("flagship release receipt does not report all product-canon docs present")

    release_docs, all_release_docs_present = _present_path_nodes(flagship.get("release_docs"))
    if release_docs != list(REQUIRED_RELEASE_DOCS) or not all_release_docs_present:
        issues.append("flagship release receipt lacks the exact present release-doc nodes")

    _verify_freshness(
        browser=browser,
        flagship=flagship,
        pulse=pulse,
        receipt_commit_time=receipt_time,
        deploy_metadata_commit_time=deploy_metadata_time,
        max_age_seconds=max_age_seconds,
        now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
        issues=issues,
    )
    return list(dict.fromkeys(issues))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only fail-closed verification of canonical PropertyQuarry production deploy receipts."
    )
    parser.add_argument("--root", type=Path, required=True, help="Canonical PropertyQuarry Git worktree root.")
    parser.add_argument("--expected-head", required=True, help="Exact clean manifest/pulse deploy metadata commit.")
    parser.add_argument("--expected-receipt-commit", required=True, help="Exact canonical receipt-only commit.")
    parser.add_argument("--expected-code-parent", required=True, help="Exact immutable code parent commit.")
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=MAX_RECEIPT_AGE_SECONDS,
        help=f"Maximum receipt and metadata-commit age, capped at {MAX_RECEIPT_AGE_SECONDS}.",
    )
    args = parser.parse_args()

    issues = verify_deploy_receipts(
        root=args.root,
        expected_head=args.expected_head,
        expected_receipt_commit=args.expected_receipt_commit,
        expected_code_parent=args.expected_code_parent,
        max_age_seconds=args.max_age_seconds,
    )
    if issues:
        print(json.dumps({"status": "blocked", "issues": issues}, indent=2))
        return 1
    print(
        json.dumps(
            {
                "status": "pass",
                "message": "Canonical PropertyQuarry deploy receipts match the current metadata/code commits.",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
