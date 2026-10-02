#!/usr/bin/env python3
"""Verify global-governance release assets without creating release evidence."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import propertyquarry_global_experience_gate as experience_gate  # noqa: E402
from scripts import propertyquarry_global_launch_terminal as global_terminal  # noqa: E402
from scripts import propertyquarry_gold_status as gold_status  # noqa: E402
from scripts import propertyquarry_jurisdiction_privacy_rights_gate as rights_gate  # noqa: E402
from scripts import propertyquarry_release_proof_baseline as release_baseline  # noqa: E402


VERIFICATION_SCHEMA = "propertyquarry.global_governance_release_assets.v1"
EXPECTED_SCHEMAS = {
    "global_experience_contract": "propertyquarry.global_experience.v1",
    "global_experience_live": "propertyquarry.global_experience_live_receipt.v1",
    "global_experience_gate": "propertyquarry.global_experience_gate.v1",
    "jurisdiction_privacy_rights_contract": "propertyquarry.jurisdiction_privacy_rights.v1",
    "jurisdiction_privacy_rights_live": "propertyquarry.jurisdiction_privacy_rights_live_receipt.v1",
    "jurisdiction_privacy_rights_gate": "propertyquarry.jurisdiction_privacy_rights_gate.v1",
}
REQUIRED_ASSETS = (
    "config/monitoring/propertyquarry_global_experience.v1.json",
    "docs/PROPERTYQUARRY_GLOBAL_EXPERIENCE_EVIDENCE.md",
    "scripts/propertyquarry_global_experience_gate.py",
    "config/compliance/propertyquarry_jurisdiction_privacy_rights.v1.json",
    "docs/PROPERTYQUARRY_JURISDICTION_PRIVACY_AND_PROVIDER_RIGHTS.md",
    "scripts/propertyquarry_jurisdiction_privacy_rights_gate.py",
    "docs/propertyquarry_global_market_envelope.v1.json",
    "docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md",
    "docs/PROPERTYQUARRY_GLOBAL_LAUNCH_TERMINAL_INSTALL.md",
    "config/monitoring/propertyquarry_flagship_operations.v1.json",
    "packaging/propertyquarry-global-launch-terminal/global-launch-terminal-bundle.v1.schema.json",
    "scripts/build_propertyquarry_global_launch_terminal_bundle.py",
    "scripts/propertyquarry_global_launch_terminal.py",
    "scripts/propertyquarry_gold_status.py",
    "RELEASE_CHECKLIST.md",
    "PRODUCT_RELEASE_CHECKLIST.md",
    ".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md",
    ".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json",
    ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
)


def _load_object(path: Path, issues: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(f"unreadable JSON asset {path}: {type(exc).__name__}")
        return {}
    if not isinstance(payload, dict):
        issues.append(f"JSON asset is not an object: {path}")
        return {}
    return payload


def _contains_all(
    path: Path,
    required: tuple[str, ...],
    issues: list[str],
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        issues.append(f"unreadable text asset {path}: {type(exc).__name__}")
        return
    for token in required:
        if token not in text:
            issues.append(f"{path} is missing required governance token: {token}")


def _source_only_gate_issues(root: Path, now: datetime) -> list[str]:
    issues: list[str] = []
    experience_receipt = experience_gate.build_global_experience_gate_receipt(
        contract_path=(
            root / "config/monitoring/propertyquarry_global_experience.v1.json"
        ),
        live_receipt_path=None,
        expected_commit="",
        expected_image="",
        now=now,
    )
    if experience_receipt.get("schema") != experience_gate.GATE_RECEIPT_SCHEMA:
        issues.append("global-experience source-only receipt schema changed")
    if experience_receipt.get("status") != "blocked":
        issues.append("global-experience source-only evaluation did not block")
    if experience_receipt.get("independently_attested") is not False:
        issues.append("global-experience source-only evaluation claims attestation")
    if list(experience_receipt.get("market_results") or []):
        issues.append("global-experience source-only evaluation invented market results")
    if not any(
        "live_receipt" in str(blocker) and "required" in str(blocker)
        for blocker in list(experience_receipt.get("blockers") or [])
    ):
        issues.append("global-experience source-only evaluation lacks the live-receipt blocker")

    rights_receipt = rights_gate.build_gate(
        contract_path=(
            root
            / "config/compliance/propertyquarry_jurisdiction_privacy_rights.v1.json"
        ),
        market_envelope_path=(
            root / "docs/propertyquarry_global_market_envelope.v1.json"
        ),
        live_receipt_path=None,
        expected_release_sha="",
        expected_image_digest="",
        now=now,
    )
    if rights_receipt.get("schema") != rights_gate.GATE_SCHEMA:
        issues.append("jurisdiction/privacy/provider-rights source-only receipt schema changed")
    if rights_receipt.get("status") != "blocked":
        issues.append("jurisdiction/privacy/provider-rights source-only evaluation did not block")
    if not any(
        "live jurisdiction/privacy/provider-rights receipt is required" in str(blocker)
        for blocker in list(rights_receipt.get("blockers") or [])
    ):
        issues.append(
            "jurisdiction/privacy/provider-rights source-only evaluation lacks the live-receipt blocker"
        )
    for section in ("source_contract", "market_envelope"):
        row = rights_receipt.get(section)
        if not isinstance(row, Mapping) or row.get("status") != "pass":
            issues.append(
                f"jurisdiction/privacy/provider-rights source-only evaluation has invalid {section}"
            )
    return issues


def _gold_contract_issues(root: Path) -> list[str]:
    issues: list[str] = []
    if (
        gold_status.GLOBAL_EXPERIENCE_GATE_RECEIPT_SCHEMA
        != experience_gate.GATE_RECEIPT_SCHEMA
    ):
        issues.append("Gold global-experience receipt schema differs from its producer")
    if (
        gold_status.JURISDICTION_PRIVACY_RIGHTS_GATE_RECEIPT_SCHEMA
        != rights_gate.GATE_SCHEMA
    ):
        issues.append(
            "Gold jurisdiction/privacy/provider-rights receipt schema differs from its producer"
        )

    parameters = inspect.signature(gold_status.build_gold_status_receipt).parameters
    for parameter in (
        "global_experience_receipt_path",
        "jurisdiction_privacy_rights_receipt_path",
    ):
        if parameter not in parameters:
            issues.append(f"Gold build contract is missing parameter: {parameter}")

    helper_cases = (
        (
            "global-experience",
            gold_status._global_experience_launch_status,
        ),
        (
            "jurisdiction/privacy/provider-rights",
            gold_status._jurisdiction_privacy_rights_launch_status,
        ),
    )
    for label, helper in helper_cases:
        required_ok, required_details = helper(
            {},
            receipt_present=False,
            required=True,
            expected_release_commit_sha="",
            expected_release_image_digest="",
            now=None,
            max_age_hours=24.0,
        )
        optional_ok, optional_details = helper(
            {},
            receipt_present=False,
            required=False,
            expected_release_commit_sha="",
            expected_release_image_digest="",
            now=None,
            max_age_hours=24.0,
        )
        if required_ok or required_details.get("status") != "missing":
            issues.append(f"Gold does not fail closed on missing required {label} evidence")
        if not optional_ok or optional_details.get("status") != "not_required":
            issues.append(f"Gold does not preserve optional {label} health semantics")

    _contains_all(
        root / "scripts/propertyquarry_gold_status.py",
        (
            '"--global-experience-receipt"',
            '"--jurisdiction-privacy-rights-receipt"',
            '"area": "global_experience"',
            '"area": "jurisdiction_privacy_rights"',
            "not core_launch_governance_required or global_experience_ok",
            "or jurisdiction_privacy_rights_ok",
        ),
        issues,
    )
    return issues


def _terminal_contract_issues(root: Path) -> list[str]:
    issues: list[str] = []
    expected_command = global_terminal.GLOBAL_LAUNCH_TERMINAL_COMMAND
    if release_baseline.GLOBAL_LAUNCH_TERMINAL_COMMAND != expected_command:
        issues.append("release baseline global terminal command differs from wrapper")
    for label, observed, expected in (
        (
            "manifest schema",
            release_baseline.GLOBAL_LAUNCH_TERMINAL_MANIFEST_SCHEMA,
            global_terminal.MANIFEST_SCHEMA,
        ),
        (
            "manifest path",
            release_baseline.GLOBAL_LAUNCH_TERMINAL_MANIFEST_PATH,
            global_terminal.GLOBAL_LAUNCH_MANIFEST_PATH,
        ),
        (
            "result schema",
            release_baseline.GLOBAL_LAUNCH_TERMINAL_RESULT_SCHEMA,
            global_terminal.RESULT_SCHEMA,
        ),
        (
            "bundle schema",
            release_baseline.GLOBAL_LAUNCH_TERMINAL_BUNDLE_SCHEMA,
            global_terminal.INSTALLED_BUNDLE_SCHEMA,
        ),
    ):
        if observed != expected:
            issues.append(f"release baseline global terminal {label} differs from wrapper")
    seed = _load_object(
        root / ".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json",
        issues,
    )
    launch_contract = seed.get("global_launch_contract") if seed else None
    if (
        not isinstance(launch_contract, Mapping)
        or launch_contract.get("terminal_command") != expected_command
    ):
        issues.append("global launch seed does not name the authoritative terminal command")
    if isinstance(launch_contract, Mapping):
        issues.extend(
            release_baseline.approved_global_launch_contract_blockers(
                dict(launch_contract)
            )
        )
    if set(global_terminal.GLOBAL_GOVERNANCE_RECEIPT_KEYS) != {
        "global_market_envelope",
        "incident_support",
        "global_experience",
        "jurisdiction_privacy_rights",
    }:
        issues.append("global terminal governance receipt set changed")
    if set(global_terminal.RAW_OBSERVABILITY_FLAGS) != {
        "slo_metrics_snapshot",
        "slo_metrics_probe",
        "monitoring_runtime_receipt",
        "prometheus_range_receipt",
        "prometheus_range_response",
        "alert_delivery_receipt",
    }:
        issues.append("global terminal raw-observability set changed")
    if set(global_terminal.TERMINAL_AUTHORITY_KEYS) != {
        "release_preflight",
        "disaster_recovery",
        "capacity",
        "observability_operations",
        "controller_attestation",
    }:
        issues.append("global terminal authority set changed")
    if any(
        name not in global_terminal.CORE_RECEIPT_FLAGS
        for name in global_terminal.GLOBAL_GOVERNANCE_RECEIPT_KEYS
    ):
        issues.append("global terminal omits a governance receipt from Core Gold argv")

    try:
        gold_source = (root / "scripts/propertyquarry_gold_status.py").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError) as exc:
        issues.append(f"unreadable Gold source for terminal ABI: {type(exc).__name__}")
        gold_source = ""
    for flag in (
        *global_terminal.CORE_RECEIPT_FLAGS.values(),
        *global_terminal.RAW_OBSERVABILITY_FLAGS.values(),
        "--expected-release-sha",
        "--expected-image-digest",
        "--expected-public-origin",
        "--expected-teable-origin",
        "--expected-teable-base-id-sha256",
        "--expected-rybbit-origin",
        "--expected-rybbit-site-id-sha256",
        "--expected-evidence-overlay-phase",
        "--required-browser-engines",
        "--launch-evidence-dir",
        "--write",
        "--require-launch-evidence",
        "--fail-on-blocked",
    ):
        if f'"{flag}"' not in gold_source:
            issues.append(f"Gold parser does not expose terminal ABI flag: {flag}")

    _contains_all(
        root / "scripts/propertyquarry_global_launch_terminal.py",
        (
            "propertyquarry.global_launch_terminal_manifest.v1",
            "propertyquarry.global_launch_terminal_controller_attestation.v1",
            "propertyquarry.observability_operations_receipt.v1",
            "propertyquarry.global_launch_invocation_contract.v1",
            "propertyquarry.global_launch_terminal_bundle.v1",
            "/usr/libexec/propertyquarry/propertyquarry-global-launch-terminal",
            "propertyquarry-production",
            "load_evidence_challenge",
            "verify_authenticated_payload",
            "attested_artifact_digests",
            '"product_data"',
            '"observability_operations"',
            '"traceparent"',
            '"runbooks"',
            "shell=False",
            "stdout=subprocess.DEVNULL",
            "stderr=subprocess.DEVNULL",
            "timeout=GOLD_TIMEOUT_SECONDS",
            "--profile",
            "--claim-scope",
            "--require-launch-evidence",
            "--fail-on-blocked",
        ),
        issues,
    )
    _contains_all(
        root / "docs/PROPERTYQUARRY_GLOBAL_LAUNCH_TERMINAL_INSTALL.md",
        (
            expected_command,
            "non-authoritative developer validation",
            "root-owned",
            "never installs",
            "file descriptor",
        ),
        issues,
    )
    return issues


def _generated_source_proof_issues(root: Path, issues: list[str]) -> None:
    receipt = _load_object(
        root / ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
        issues,
    )
    if not receipt:
        return
    source_binding = receipt.get("source_binding")
    if source_binding is None and receipt.get("status") != "blocked":
        issues.append("uncommitted generated source proof is not blocked")
    live = receipt.get("live_readiness")
    global_live = receipt.get("global_launch_readiness")
    if not isinstance(live, Mapping) or live.get("status") != "not_evaluated":
        issues.append("generated source proof overclaims final live readiness")
    if (
        not isinstance(global_live, Mapping)
        or global_live.get("status") != "not_evaluated"
        or global_live.get("source_browser_checkpoint_is_sufficient") is not False
    ):
        issues.append("generated source proof overclaims global launch readiness")
    if (
        isinstance(global_live, Mapping)
        and global_live.get("terminal_command")
        != global_terminal.GLOBAL_LAUNCH_TERMINAL_COMMAND
    ):
        issues.append("generated source proof terminal command differs from wrapper")
    launch_contract = receipt.get("global_launch_contract")
    if (
        not isinstance(launch_contract, Mapping)
        or launch_contract.get("terminal_command")
        != global_terminal.GLOBAL_LAUNCH_TERMINAL_COMMAND
    ):
        issues.append("generated source proof embeds a stale global terminal command")


def verify_global_governance_assets(
    root: Path = ROOT,
    *,
    now: datetime | None = None,
) -> list[str]:
    root = root.resolve()
    issues: list[str] = []
    for relative in REQUIRED_ASSETS:
        if not (root / relative).is_file():
            issues.append(f"missing required global-governance asset: {relative}")
    if issues:
        return issues

    experience_contract = _load_object(
        root / "config/monitoring/propertyquarry_global_experience.v1.json",
        issues,
    )
    rights_contract = _load_object(
        root
        / "config/compliance/propertyquarry_jurisdiction_privacy_rights.v1.json",
        issues,
    )
    observed_schemas = {
        "global_experience_contract": experience_gate.CONTRACT_SCHEMA,
        "global_experience_live": experience_gate.LIVE_RECEIPT_SCHEMA,
        "global_experience_gate": experience_gate.GATE_RECEIPT_SCHEMA,
        "jurisdiction_privacy_rights_contract": rights_gate.CONTRACT_SCHEMA,
        "jurisdiction_privacy_rights_live": rights_gate.LIVE_RECEIPT_SCHEMA,
        "jurisdiction_privacy_rights_gate": rights_gate.GATE_SCHEMA,
    }
    for name, expected in EXPECTED_SCHEMAS.items():
        if observed_schemas.get(name) != expected:
            issues.append(f"governance schema constant changed: {name}")
    if experience_contract.get("schema") != EXPECTED_SCHEMAS[
        "global_experience_contract"
    ]:
        issues.append("global-experience source contract schema changed")
    if experience_contract.get("source_contract_status") != (
        "defined_not_live_evidence"
    ):
        issues.append("global-experience source contract no longer denies live authority")
    issues.extend(
        f"global-experience contract: {error}"
        for error in experience_gate.validate_contract(experience_contract)
    )
    if rights_contract.get("schema") != EXPECTED_SCHEMAS[
        "jurisdiction_privacy_rights_contract"
    ]:
        issues.append("jurisdiction/privacy/provider-rights source contract schema changed")
    if rights_contract.get("source_contract_status") != "defined":
        issues.append("jurisdiction/privacy/provider-rights source contract is not defined")
    issues.extend(
        f"jurisdiction/privacy/provider-rights contract: {error}"
        for error in rights_gate.validate_source_contract(rights_contract)
    )

    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issues.extend(_source_only_gate_issues(root, observed_now))
    issues.extend(_gold_contract_issues(root))
    issues.extend(_terminal_contract_issues(root))
    _generated_source_proof_issues(root, issues)

    documentation_tokens = {
        "docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md": (
            "propertyquarry_global_experience_gate.py",
            "propertyquarry_jurisdiction_privacy_rights_gate.py",
            "no governed live global-experience receipt",
            "no governed live",
            "jurisdiction/privacy/provider-rights receipt",
            "propertyquarry_global_launch_terminal.py",
            global_terminal.GLOBAL_LAUNCH_MANIFEST_PATH,
            "W3C trace continuity",
            global_terminal.RESULT_SCHEMA,
        ),
        "RELEASE_CHECKLIST.md": (
            "propertyquarry_global_experience_gate.py",
            "propertyquarry_jurisdiction_privacy_rights_gate.py",
            "global-governance gates",
            "propertyquarry_global_launch_terminal.py",
            global_terminal.GLOBAL_LAUNCH_MANIFEST_PATH,
            "observability-operations",
            global_terminal.RESULT_SCHEMA,
        ),
        "PRODUCT_RELEASE_CHECKLIST.md": (
            "global-experience",
            "jurisdiction/privacy/provider-rights",
            "propertyquarry_global_launch_terminal.py",
            global_terminal.GLOBAL_LAUNCH_MANIFEST_PATH,
            "W3C trace",
            global_terminal.RESULT_SCHEMA,
        ),
        ".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md": (
            "global-experience gate",
            "jurisdiction/privacy/provider-rights gate",
            "propertyquarry_global_launch_terminal.py",
            global_terminal.GLOBAL_LAUNCH_MANIFEST_PATH,
            "W3C",
            "Gold-result digests",
        ),
    }
    for relative, tokens in documentation_tokens.items():
        _contains_all(root / relative, tokens, issues)
    return list(dict.fromkeys(issues))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    issues = verify_global_governance_assets(args.root)
    result = {
        "schema": VERIFICATION_SCHEMA,
        "status": "pass" if not issues else "blocked",
        "issue_count": len(issues),
        "issues": issues,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
