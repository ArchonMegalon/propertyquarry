#!/usr/bin/env python3
"""Render one fail-closed PropertyQuarry local-Docker operator truth view."""

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

try:
    from propertyquarry_advanced_visual_gold_binding import (
        verify_advanced_visual_binding_receipt,
    )
except ModuleNotFoundError:
    from scripts.propertyquarry_advanced_visual_gold_binding import (
        verify_advanced_visual_binding_receipt,
    )

try:
    from propertyquarry_public_launch_authority import (
        PUBLIC_LAUNCH_RECEIPT_CONTRACT,
        PUBLIC_LAUNCH_RECEIPT_PATH,
        PUBLIC_LAUNCH_REQUIREMENTS,
        PUBLIC_LAUNCH_TRUST_STORE_PATH,
        TRUST_STORE_ENV,
        PublicLaunchAuthorityError,
        verify_public_launch_authority,
    )
except ModuleNotFoundError:
    from scripts.propertyquarry_public_launch_authority import (
        PUBLIC_LAUNCH_RECEIPT_CONTRACT,
        PUBLIC_LAUNCH_RECEIPT_PATH,
        PUBLIC_LAUNCH_REQUIREMENTS,
        PUBLIC_LAUNCH_TRUST_STORE_PATH,
        TRUST_STORE_ENV,
        PublicLaunchAuthorityError,
        verify_public_launch_authority,
    )

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
SCHEMA = "propertyquarry.launch_room.v3"
DEFAULT_DEPLOYMENT_RECEIPT = Path(
    "state/release/propertyquarry-local-deployment.v1.json"
)
DEFAULT_ADVANCED_VISUAL_BINDING_RECEIPT = Path(
    "_completion/property_gold_status/advanced-visual-candidate-binding.json"
)
ADVANCED_VISUAL_SOURCE_RECEIPTS = {
    "walkthrough_quality": Path(
        "_completion/smoke/property-live-walkthrough-quality-release-gate.json"
    ),
    "walkthrough_provider_proof": Path(
        "_completion/smoke/property-live-walkthrough-provider-proof-release-gate.json"
    ),
    "scene_video_readiness": Path(
        "_completion/scene_video_readiness/release-gate.json"
    ),
    "scene_video_readiness_verifier": Path(
        "_completion/scene_video_readiness/release-gate-verifier.json"
    ),
    "scene_video_runtime_status": Path(
        "_completion/scene_video_readiness/runtime-status.json"
    ),
    "scene_video_provider_refresh_packet": Path(
        "_completion/scene_video_readiness/provider-refresh-packet.json"
    ),
    "scene_video_provider_refresh_packet_verifier": Path(
        "_completion/scene_video_readiness/provider-refresh-packet-verifier.json"
    ),
    "privacy": Path(
        "_completion/security/property-security-posture-release-gate.json"
    ),
}
DEPLOYMENT_SCHEMA = "propertyquarry.local_docker_deployment.v1"
DEFAULT_PUBLIC_LAUNCH_AUTHORITY_RECEIPT = PUBLIC_LAUNCH_RECEIPT_PATH
PUBLIC_LAUNCH_EVIDENCE_HANDOFF: dict[str, dict[str, object]] = {
    "google_play_public_launch": {
        "evidence_origin": "external_google_play_console",
        "evidence_contract": "google_play_public_launch_external_receipt",
        "repository_verifier": None,
        "required_proofs": [
            "production_access_granted",
            "production_release_active_for_austria",
            "app_content_and_store_listing_complete",
            "install_available_without_internal_tester_invitation",
        ],
    },
    "paid_billing_safe_handoff": {
        "evidence_origin": "external_paid_checkout_canary",
        "evidence_contract": "paid_billing_safe_handoff_external_receipt",
        "repository_verifier": None,
        "required_proofs": [
            "paid_plan_checkout_configured",
            "authenticated_principal_preserved_without_second_login",
            "signed_webhook_verified_and_idempotent",
            "entitlement_grant_and_cancellation_verified",
        ],
    },
    "encrypted_off_host_disaster_recovery": {
        "evidence_origin": "propertyquarry_release_gate",
        "evidence_contract": "propertyquarry.postgres_dr_receipt.v3:release_gate",
        "repository_verifier": (
            "scripts/propertyquarry_postgres_dr.py release-gate"
        ),
        "required_proofs": [
            "encrypted_backup_bound_to_exact_release",
            "immutable_off_host_object_verified",
            "provider_retrieval_attested",
            "disposable_restore_rpo_rto_and_critical_data_verified",
        ],
    },
}


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


def _deployment_status(
    path: Path,
    runtime_sha: str,
    *,
    envelope_head_sha: str,
) -> dict[str, object]:
    if not path.is_file():
        return {
            "status": "missing_local_docker_deployment_receipt",
            "local_runtime_ready": False,
            "receipt_path": str(path),
        }
    receipt = _object(path)
    authority = (
        dict(receipt.get("authority") or {})
        if isinstance(receipt.get("authority"), dict)
        else {}
    )
    services = (
        dict(receipt.get("services") or {})
        if isinstance(receipt.get("services"), dict)
        else {}
    )
    required_services = {
        "propertyquarry-api",
        "propertyquarry-migrate",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
        "propertyquarry-db",
        "propertyquarry-cloudflared",
    }
    passed = (
        receipt.get("schema") == DEPLOYMENT_SCHEMA
        and receipt.get("passed") is True
        and receipt.get("secret_values_recorded") is False
        and receipt.get("runtime_commit_sha") == runtime_sha
        and receipt.get("envelope_head_sha") == envelope_head_sha
        and authority.get("scope") == "local_docker"
        and authority.get("proof_plane") == "local_docker_operator_receipts"
        and authority.get("github_actions_used") is False
        and authority.get("canonical_repository")
        == "ArchonMegalon/propertyquarry"
        and set(services) == required_services
        and dict(receipt.get("local_probe") or {}).get("status") == "pass"
    )
    return {
        "status": (
            "healthy_exact_candidate_local_docker"
            if passed
            else "blocked_local_docker_receipt_invalid_or_stale"
        ),
        "local_runtime_ready": passed,
        "receipt_path": str(path),
        "runtime_candidate_bound": receipt.get("runtime_commit_sha") == runtime_sha,
        "envelope_head_bound": receipt.get("envelope_head_sha") == envelope_head_sha,
        "compose_project": dict(receipt.get("compose") or {}).get("project"),
        "service_count": len(services),
        "observed_at": receipt.get("observed_at"),
        "release_image_digest": str(
            dict(receipt.get("local_probe") or {})
            .get("release_identity", {})
            .get("release_image_digest", "")
        ),
    }


def _public_launch_status(
    path: Path,
    *,
    envelope_head_sha: str,
    runtime_sha: str,
    image_digest: str,
) -> dict[str, object]:
    requirement_blockers = [
        f"{requirement}_authority_unverified"
        for requirement in PUBLIC_LAUNCH_REQUIREMENTS
    ]
    if not path.is_file():
        return {
            "status": "blocked_external_authority_receipt_missing",
            "authority_passed": False,
            "receipt_path": str(path),
            "blockers": [
                "external_public_launch_authority_receipt_missing",
                *requirement_blockers,
            ],
        }
    try:
        verified = verify_public_launch_authority(
            path,
            expected_envelope_head_sha=envelope_head_sha,
            expected_runtime_commit_sha=runtime_sha,
            expected_release_image_digest=image_digest,
        )
    except PublicLaunchAuthorityError as exc:
        return {
            "status": "blocked_external_authority_receipt_invalid",
            "authority_passed": False,
            "receipt_path": str(path),
            "verifier_error": exc.reason,
            "blockers": [
                f"external_public_launch_authority:{exc.reason}",
                *requirement_blockers,
            ],
        }
    return {
        "status": "verified_external_public_launch_authority",
        "authority_passed": True,
        "receipt_path": str(path),
        "verification": verified.as_dict(),
        "blockers": [],
    }


def _public_launch_authority_handoff(
    *,
    envelope_head_sha: str,
    runtime_sha: str,
    image_digest: str,
    local_runtime_ready: bool,
    authority_passed: bool,
) -> dict[str, object]:
    receipt_installed = PUBLIC_LAUNCH_RECEIPT_PATH.is_file()
    trust_store_installed = PUBLIC_LAUNCH_TRUST_STORE_PATH.is_file()
    configured_trust_store = str(os.getenv(TRUST_STORE_ENV) or "").strip()
    trust_store_environment_configured = (
        configured_trust_store == str(PUBLIC_LAUNCH_TRUST_STORE_PATH)
    )
    verification_material_present = bool(
        receipt_installed
        and trust_store_installed
        and trust_store_environment_configured
    )
    envelope_bound = re.fullmatch(r"[0-9a-f]{40}", envelope_head_sha) is not None
    runtime_bound = re.fullmatch(r"[0-9a-f]{40}", runtime_sha) is not None
    image_bound = re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is not None
    binding_values_well_formed = envelope_bound and runtime_bound and image_bound
    exact_candidate_bound = bool(
        local_runtime_ready and binding_values_well_formed
    )
    ready_for_external_issuance = bool(
        exact_candidate_bound and not authority_passed
    )
    if authority_passed:
        status = "verified"
    elif not local_runtime_ready:
        status = "blocked_local_runtime_precondition"
    elif not exact_candidate_bound:
        status = "blocked_exact_candidate_binding_incomplete"
    else:
        status = "ready_for_external_authority"
    return {
        "status": status,
        "ready_for_external_issuance": ready_for_external_issuance,
        "authority_scope": "external_global_governance",
        "local_substitution_allowed": False,
        "receipt_contract": PUBLIC_LAUNCH_RECEIPT_CONTRACT,
        "receipt_path": str(PUBLIC_LAUNCH_RECEIPT_PATH),
        "verification_material": {
            "receipt_installed": receipt_installed,
            "trust_store_installed": trust_store_installed,
            "trust_store_environment_configured": (
                trust_store_environment_configured
            ),
            "present": verification_material_present,
        },
        "trust_store": {
            "environment_variable": TRUST_STORE_ENV,
            "path": str(PUBLIC_LAUNCH_TRUST_STORE_PATH),
            "caller_selected_path_allowed": False,
        },
        "exact_candidate": {
            "bindings_complete": exact_candidate_bound,
            "binding_values_well_formed": binding_values_well_formed,
            "deployment_proven": local_runtime_ready,
            "envelope_head_sha": envelope_head_sha or "unavailable",
            "runtime_commit_sha": runtime_sha or "unavailable",
            "release_image_digest": image_digest or "unavailable",
        },
        "receipt_constraints": {
            "canonical_repository": "ArchonMegalon/propertyquarry",
            "passed_required": True,
            "secret_values_recorded_required": False,
            "canonical_json_required": True,
            "external_signature_required": True,
            "fixed_receipt_path_required": True,
            "fixed_trust_store_required": True,
            "candidate_binding_required": True,
        },
        "required_evidence": {
            requirement: {
                **PUBLIC_LAUNCH_EVIDENCE_HANDOFF[requirement],
                "status_required": "pass",
                "evidence_ref_contract": "nonempty_bounded_nonsecret_reference",
                "evidence_sha256_contract": "sha256:<64_lowercase_hex>",
                "authority_state": (
                    "verified" if authority_passed else "unverified"
                ),
            }
            for requirement in PUBLIC_LAUNCH_REQUIREMENTS
        },
    }


def _advanced_visual_status(
    *,
    root: Path,
    path: Path,
    runtime_sha: str,
    image_digest: str,
) -> dict[str, object]:
    if not path.is_file():
        return {
            "status": "unavailable_unbound_producer_receipts",
            "production_claim": False,
        }
    try:
        receipt = _object(path)
        errors = verify_advanced_visual_binding_receipt(
            receipt,
            expected_release_commit_sha=runtime_sha,
            expected_release_image_digest=image_digest,
            source_receipt_paths={
                name: root / source_path
                for name, source_path in ADVANCED_VISUAL_SOURCE_RECEIPTS.items()
            },
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        errors = [f"binding_verifier_error:{type(exc).__name__}"]
    return {
        "status": "bound" if not errors else "unavailable_unbound_producer_receipts",
        "production_claim": not errors,
        "receipt_path": str(path),
        "errors": errors,
    }


def build_launch_room(
    root: Path = ROOT,
    *,
    deployment_receipt_path: Path | None = None,
    advanced_visual_binding_receipt_path: Path | None = None,
    public_launch_authority_receipt_path: Path | None = None,
) -> dict[str, object]:
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
    if deployment_receipt_path is None:
        configured = os.environ.get("PROPERTYQUARRY_LOCAL_DEPLOYMENT_RECEIPT", "")
        deployment_receipt_path = (
            Path(configured)
            if configured
            else root / DEFAULT_DEPLOYMENT_RECEIPT
        )
    elif not deployment_receipt_path.is_absolute():
        deployment_receipt_path = root / deployment_receipt_path
    deployment = _deployment_status(
        deployment_receipt_path,
        runtime_sha,
        envelope_head_sha=head_sha,
    )
    deployment_green = deployment["local_runtime_ready"] is True
    local_runtime_ready = candidate_proof_green and deployment_green and not dirty
    if advanced_visual_binding_receipt_path is None:
        advanced_visual_binding_receipt_path = (
            root / DEFAULT_ADVANCED_VISUAL_BINDING_RECEIPT
        )
    elif not advanced_visual_binding_receipt_path.is_absolute():
        advanced_visual_binding_receipt_path = (
            root / advanced_visual_binding_receipt_path
        )
    advanced_visual = _advanced_visual_status(
        root=root,
        path=advanced_visual_binding_receipt_path,
        runtime_sha=runtime_sha,
        image_digest=str(deployment.get("release_image_digest") or ""),
    )
    advanced_visual_bound = advanced_visual.get("production_claim") is True
    if public_launch_authority_receipt_path is None:
        configured_public_launch = os.environ.get(
            "PROPERTYQUARRY_PUBLIC_LAUNCH_AUTHORITY_RECEIPT", ""
        )
        public_launch_authority_receipt_path = (
            Path(configured_public_launch)
            if configured_public_launch
            else root / DEFAULT_PUBLIC_LAUNCH_AUTHORITY_RECEIPT
        )
    elif not public_launch_authority_receipt_path.is_absolute():
        public_launch_authority_receipt_path = (
            root / public_launch_authority_receipt_path
        )
    public_launch = _public_launch_status(
        public_launch_authority_receipt_path,
        envelope_head_sha=head_sha,
        runtime_sha=runtime_sha,
        image_digest=str(deployment.get("release_image_digest") or ""),
    )
    public_launch_ready = bool(
        local_runtime_ready and public_launch.get("authority_passed") is True
    )
    public_launch["ready"] = public_launch_ready
    public_launch["local_runtime_ready"] = local_runtime_ready
    public_launch["authority_handoff"] = _public_launch_authority_handoff(
        envelope_head_sha=head_sha,
        runtime_sha=runtime_sha,
        image_digest=str(deployment.get("release_image_digest") or ""),
        local_runtime_ready=local_runtime_ready,
        authority_passed=public_launch.get("authority_passed") is True,
    )
    advanced_visual["local_runtime_claim"] = bool(
        local_runtime_ready and advanced_visual_bound
    )
    advanced_visual["production_claim"] = bool(
        public_launch_ready and advanced_visual_bound
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
        "release_proof_plane": {
            "status": "local_docker_operator_receipts",
            "github_actions_used": False,
        },
        "core_gold": {
            "status": (
                "local_docker_deployed"
                if candidate_proof_green and deployment_green
                else "candidate_eligible_local_deployment_missing"
                if candidate_proof_green
                else "blocked_candidate_proof"
            ),
            "local_runtime_claim": local_runtime_ready,
            "production_claim": public_launch_ready,
        },
        "advanced_visual_gold": advanced_visual,
        "live_deployment": deployment,
        "public_edge": {
            "status": (
                "local_cloudflared_container_running"
                if deployment_green
                else "blocked_until_local_docker_receipt_passes"
            ),
            "network_freshness_proven": False,
        },
        "public_launch": public_launch,
        "next_action": (
            "Commit, rebuild, and redeploy the local Docker stack."
            if dirty
            else "Run scripts/deploy_propertyquarry.sh on the local Docker host."
            if not deployment_green
            else "Provide passing Google Play, paid-billing handoff, and encrypted off-host restore evidence to the external governance authority, then install its signed receipt at the fixed release-control path."
        ),
        "local_runtime_ready": local_runtime_ready,
        "production_launch_ready": public_launch_ready,
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
        ("Release proof plane", dict(report["release_proof_plane"])["status"]),
        ("Core Gold", dict(report["core_gold"])["status"]),
        (
            "Advanced Visual Gold",
            dict(report["advanced_visual_gold"])["status"],
        ),
        ("Live deployment", dict(report["live_deployment"])["status"]),
        ("Public edge", dict(report["public_edge"])["status"]),
        (
            "Local runtime",
            "READY" if report["local_runtime_ready"] else "BLOCKED",
        ),
        (
            "Public launch",
            "READY" if report["production_launch_ready"] else "BLOCKED",
        ),
        (
            "External authority handoff",
            dict(dict(report["public_launch"])["authority_handoff"])["status"],
        ),
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
    parser.add_argument(
        "--deployment-receipt",
        type=Path,
        help=(
            "Local Docker deployment receipt; defaults to "
            "state/release/propertyquarry-local-deployment.v1.json"
        ),
    )
    parser.add_argument(
        "--advanced-visual-binding-receipt",
        type=Path,
        help=(
            "Exact-candidate Advanced Visual Gold binding; defaults to "
            "_completion/property_gold_status/advanced-visual-candidate-binding.json"
        ),
    )
    parser.add_argument(
        "--public-launch-authority-receipt",
        type=Path,
        help=(
            "Reserved external public-launch receipt path; defaults to the "
            "fixed release-control path outside the checkout. Overrides are "
            "diagnostic only and cannot authorize public launch."
        ),
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--write", type=Path)
    parser.add_argument(
        "--require-local-runtime-ready",
        action="store_true",
        help=(
            "Exit non-zero unless exact candidate proof, the exact-envelope "
            "local Docker deployment receipt, and the clean worktree pass."
        ),
    )
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help=(
            "Exit non-zero unless exact candidate proof, the local Docker "
            "deployment receipt, the clean worktree, and all externally "
            "authorized public-launch prerequisites pass."
        ),
    )
    args = parser.parse_args(argv)
    try:
        report = build_launch_room(
            args.root,
            deployment_receipt_path=args.deployment_receipt,
            advanced_visual_binding_receipt_path=(
                args.advanced_visual_binding_receipt
            ),
            public_launch_authority_receipt_path=(
                args.public_launch_authority_receipt
            ),
        )
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
    if (
        args.require_local_runtime_ready
        and report.get("local_runtime_ready") is not True
    ):
        print(
            "launch-room local runtime readiness is blocked",
            file=sys.stderr,
        )
        return 1
    if (
        args.require_production_ready
        and report.get("production_launch_ready") is not True
    ):
        print(
            "launch-room production readiness is blocked",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
