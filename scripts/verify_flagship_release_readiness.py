#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

if __package__:
    from .materialize_ea_flagship_release_gate import browser_receipt_pass_blockers
    from .propertyquarry_release_receipt_binding import (
        CANONICAL_BROWSER_RECEIPT,
        CANONICAL_FLAGSHIP_RECEIPT,
        ReleaseBindingError,
        StableFileSnapshot,
        build_source_binding,
        git_blob_oid_bytes,
        read_stable_regular_file,
        sha256_bytes,
    )
else:
    from materialize_ea_flagship_release_gate import browser_receipt_pass_blockers
    from propertyquarry_release_receipt_binding import (
        CANONICAL_BROWSER_RECEIPT,
        CANONICAL_FLAGSHIP_RECEIPT,
        ReleaseBindingError,
        StableFileSnapshot,
        build_source_binding,
        git_blob_oid_bytes,
        read_stable_regular_file,
        sha256_bytes,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PULSE = ROOT / ".codex-design" / "product" / "WEEKLY_PRODUCT_PULSE.generated.json"
DEFAULT_FLAGSHIP_RECEIPT = ROOT / ".codex-design" / "product" / "EA_FLAGSHIP_RELEASE_GATE.generated.json"
DEFAULT_BROWSER_PROOF = ROOT / ".codex-studio" / "published" / "EA_BROWSER_WORKFLOW_PROOF.generated.json"
DEFAULT_FLAGSHIP_SEED = ROOT / ".codex-design" / "repo" / "EA_FLAGSHIP_RELEASE_GATE.json"
DEFAULT_JOURNEY_GATES = Path("/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json")
DEFAULT_IMPLEMENTATION_SCOPE = ROOT / ".codex-design" / "repo" / "IMPLEMENTATION_SCOPE.md"

REQUIRED_RELEASE_CONTRACT_PATHS = (
    ROOT / ".codex-design" / "repo" / "EA_FLAGSHIP_TRUTH_PLANE.md",
    ROOT / ".codex-design" / "repo" / "EA_FLAGSHIP_RELEASE_GATE.json",
    ROOT / ".codex-design" / "repo" / "IMPLEMENTATION_SCOPE.md",
    ROOT / ".codex-design" / "ea" / "START_HERE.md",
    ROOT / ".codex-design" / "ea" / "SURFACE_DESIGN_SYSTEM.md",
    ROOT / ".codex-design" / "ea" / "LTD_INTEGRATION_MAP.md",
    ROOT / ".codex-design" / "product" / "EA_FLAGSHIP_RELEASE_GATE.generated.json",
    ROOT / ".codex-design" / "product" / "PUBLIC_MEDIA_AND_GUIDE_ASSET_POLICY.md",
    ROOT / ".codex-design" / "product" / "PUBLIC_CONCIERGE_WORKFLOWS.yaml",
    ROOT / ".codex-design" / "product" / "WEEKLY_PRODUCT_PULSE.generated.json",
    ROOT / ".codex-studio" / "published" / "EA_BROWSER_WORKFLOW_PROOF.generated.json",
)


class _DuplicateJSONKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJSONKey(key)
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _canonical_regular_path(path: Path) -> Path:
    lexical = Path(os.path.abspath(os.path.normpath(path)))
    resolved = lexical.resolve(strict=True)
    if resolved != lexical or not stat.S_ISREG(lexical.lstat().st_mode):
        raise ValueError(f"release evidence path is symlinked or not a regular file: {path}")
    return lexical


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            _canonical_regular_path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _json_snapshot(
    path: Path,
) -> tuple[dict[str, Any], StableFileSnapshot | None]:
    try:
        snapshot = read_stable_regular_file(path)
        payload = json.loads(
            snapshot.payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ReleaseBindingError,
        ValueError,
    ):
        return {}, None
    if type(payload) is not dict:
        return {}, snapshot
    return dict(payload), snapshot


def _json_values_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


def _object(value: object) -> dict[str, Any]:
    return value if type(value) is dict else {}


def _string(value: object) -> str:
    return value.strip() if type(value) is str else ""


def _nonnegative_int(value: object, *, default: int = 0) -> int | None:
    if value is None:
        return default
    if type(value) is not int or value < 0:
        return None
    return value


def _state(payload: dict[str, Any], key: str) -> str:
    section = _object(payload.get(key))
    return _string(section.get("state") or section.get("status")).lower()


def _validated_journey_summary(summary: object) -> dict[str, Any]:
    if type(summary) is not dict:
        return {}
    state = _string(summary.get("overall_state") or summary.get("state")).lower()
    blocked_count = _nonnegative_int(summary.get("blocked_count"))
    warning_count = _nonnegative_int(summary.get("warning_count"))
    if not state or blocked_count is None or warning_count is None:
        return {}
    return dict(summary)


def _pulse_journey_summary_snapshot(pulse: dict[str, Any], path: Path) -> dict[str, Any]:
    health = _object(pulse.get("journey_gate_health"))
    if not health:
        return {}
    supporting_signals = _object(pulse.get("supporting_signals"))
    source = _string(
        pulse.get("journey_gate_source")
        or supporting_signals.get("journey_gate_source")
    )
    if source != path.as_posix():
        return {}
    state = _string(health.get("state") or health.get("status")).lower()
    blocked_count = _nonnegative_int(health.get("blocked_count"))
    warning_count = _nonnegative_int(health.get("warning_count"))
    if not state or blocked_count is None or warning_count is None:
        return {}
    return {
        "overall_state": state,
        "blocked_count": blocked_count,
        "warning_count": warning_count,
        "source": "weekly_product_pulse_snapshot",
    }


def _journey_summary(path: Path, *, pulse: dict[str, Any]) -> dict[str, Any]:
    payload = _json(path)
    if "summary" in payload:
        return _validated_journey_summary(payload.get("summary"))
    return _pulse_journey_summary_snapshot(pulse, path)


def _text(path: Path) -> str:
    try:
        return _canonical_regular_path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def _source_binding_issues(
    *,
    candidate_root: Path,
    seed_path: Path,
    seed: dict[str, Any],
    flagship_receipt: dict[str, Any],
    browser_proof: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    observed_bindings = (
        ("flagship release receipt", flagship_receipt.get("source_binding")),
        ("browser workflow proof", browser_proof.get("source_binding")),
    )
    for label, observed in observed_bindings:
        if not isinstance(observed, dict):
            issues.append(f"{label} source_binding is missing or invalid")
    if issues:
        return issues

    try:
        candidate_root = candidate_root.resolve(strict=True)
        lexical_seed_path = Path(os.path.abspath(os.path.normpath(seed_path)))
        relative_seed_path = lexical_seed_path.relative_to(candidate_root)
        browser_seed = seed.get("browser_workflow_proof")
        evidence_sources = (
            browser_seed.get("evidence_sources")
            if isinstance(browser_seed, dict)
            else None
        )
        expected = build_source_binding(
            candidate_root,
            seed_path=relative_seed_path,
            evidence_sources=evidence_sources,
        )
    except (OSError, TypeError, ValueError, ReleaseBindingError) as exc:
        return [f"current candidate source identity is invalid: {exc}"]

    for label, observed in observed_bindings:
        if not _json_values_equal(observed, expected):
            issues.append(f"{label} source_binding does not match the current candidate identity")
    flagship_binding = flagship_receipt.get("source_binding")
    browser_binding = browser_proof.get("source_binding")
    if (
        isinstance(flagship_binding, dict)
        and isinstance(browser_binding, dict)
        and not _json_values_equal(flagship_binding, browser_binding)
    ):
        issues.append("flagship and browser source_binding values do not match")
    return issues


def _flagship_receipt_pass_blockers(
    receipt: dict[str, Any],
    seed: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    expected_version = seed.get("version")
    if (
        type(expected_version) is not int
        or type(receipt.get("version")) is not int
        or receipt.get("version") != expected_version
    ):
        blockers.append("flagship release receipt has the wrong version")
    if _string(receipt.get("kind")) != "release_receipt":
        blockers.append("flagship release receipt has the wrong kind")
    if _string(receipt.get("surface")) != _string(seed.get("surface")):
        blockers.append("flagship release receipt surface does not match the current gate seed")
    if (
        _string(receipt.get("generated_by"))
        != "scripts/materialize_ea_flagship_release_gate.py"
    ):
        blockers.append(
            "flagship release receipt was not produced by the governed materializer"
        )

    blocking_reasons = receipt.get("blocking_reasons")
    if type(blocking_reasons) is not list:
        blockers.append(
            "flagship release receipt blocking_reasons is missing or invalid"
        )
    elif blocking_reasons:
        blockers.append("flagship release receipt still reports blockers")

    current_limitations = receipt.get("current_limitations")
    if type(current_limitations) is not list:
        blockers.append(
            "flagship release receipt current_limitations is missing or invalid"
        )
    elif current_limitations:
        blockers.append("flagship release receipt still reports limitations")
    return blockers


def _receipt_chain_issues(
    *,
    candidate_root: Path,
    pulse: dict[str, Any],
    flagship_receipt: dict[str, Any],
    browser_snapshot: StableFileSnapshot | None,
    flagship_snapshot: StableFileSnapshot | None,
) -> list[str]:
    issues: list[str] = []
    browser_binding = flagship_receipt.get("browser_receipt_binding")
    if browser_snapshot is None:
        if type(browser_binding) is not dict:
            issues.append(
                "flagship browser_receipt_binding is missing or invalid"
            )
    else:
        try:
            expected_browser_binding = {
                "path": CANONICAL_BROWSER_RECEIPT.as_posix(),
                "sha256": sha256_bytes(browser_snapshot.payload),
                "git_blob_oid": git_blob_oid_bytes(
                    candidate_root,
                    browser_snapshot.payload,
                ),
            }
        except (OSError, ReleaseBindingError) as exc:
            issues.append(
                "canonical browser proof digest binding could not be "
                f"verified: {exc}"
            )
        else:
            if not _json_values_equal(
                browser_binding,
                expected_browser_binding,
            ):
                issues.append(
                    "flagship browser_receipt_binding does not match the "
                    "canonical browser proof bytes"
                )

    provenance = pulse.get("release_truth_provenance")
    if type(provenance) is not dict:
        issues.append(
            "weekly release_truth_provenance is missing or invalid"
        )
    elif flagship_snapshot is not None:
        if provenance.get("present") is not True:
            issues.append(
                "weekly release_truth_provenance does not mark the flagship "
                "receipt present"
            )
        if (
            _string(provenance.get("repo_relative_path"))
            != CANONICAL_FLAGSHIP_RECEIPT.as_posix()
        ):
            issues.append(
                "weekly release_truth_provenance does not name the canonical "
                "flagship receipt"
            )
        if (
            _string(provenance.get("sha256"))
            != sha256_bytes(flagship_snapshot.payload)
        ):
            issues.append(
                "weekly release_truth_provenance sha256 does not match the "
                "canonical flagship receipt bytes"
            )
        size_bytes = provenance.get("size_bytes")
        if (
            type(size_bytes) is not int
            or size_bytes != len(flagship_snapshot.payload)
        ):
            issues.append(
                "weekly release_truth_provenance size_bytes does not match "
                "the canonical flagship receipt bytes"
            )
    return issues


def verify(
    *,
    pulse_path: Path,
    flagship_receipt_path: Path,
    browser_proof_path: Path,
    journey_gates_path: Path,
    flagship_seed_path: Path = DEFAULT_FLAGSHIP_SEED,
    implementation_scope_path: Path = DEFAULT_IMPLEMENTATION_SCOPE,
    required_contract_paths: tuple[Path, ...] = REQUIRED_RELEASE_CONTRACT_PATHS,
    candidate_root: Path = ROOT,
) -> list[str]:
    issues: list[str] = []
    pulse, pulse_snapshot = _json_snapshot(pulse_path)
    receipt, flagship_snapshot = _json_snapshot(flagship_receipt_path)
    browser, browser_snapshot = _json_snapshot(browser_proof_path)
    seed = _json(flagship_seed_path)
    journey_summary = _journey_summary(journey_gates_path, pulse=pulse)
    implementation_scope = _text(implementation_scope_path)

    for path in required_contract_paths:
        try:
            _canonical_regular_path(path)
        except (OSError, ValueError):
            issues.append(f"required EA release contract missing: {path}")

    if not pulse:
        issues.append(f"weekly product pulse missing or invalid: {pulse_path}")
    if not receipt:
        issues.append(f"flagship release receipt missing or invalid: {flagship_receipt_path}")
    if not browser:
        issues.append(f"browser workflow proof missing or invalid: {browser_proof_path}")
    if not seed:
        issues.append(f"flagship gate seed missing or invalid: {flagship_seed_path}")
    if not journey_summary:
        issues.append(f"journey gates summary missing or invalid: {journey_gates_path}")
    issues.extend(
        _receipt_chain_issues(
            candidate_root=candidate_root,
            pulse=pulse,
            flagship_receipt=receipt,
            browser_snapshot=browser_snapshot,
            flagship_snapshot=flagship_snapshot,
        )
    )
    if seed:
        issues.extend(
            _source_binding_issues(
                candidate_root=candidate_root,
                seed_path=flagship_seed_path,
                seed=seed,
                flagship_receipt=receipt,
                browser_proof=browser,
            )
        )

    receipt_status = _string(receipt.get("status")).lower()
    browser_status = _string(
        browser.get("status") or browser.get("receipt_status")
    ).lower()
    release_health_payload = _object(pulse.get("release_health"))
    flagship_readiness_payload = _object(pulse.get("flagship_readiness"))
    release_health = _state(pulse, "release_health")
    flagship_readiness = _state(pulse, "flagship_readiness")
    journey_health = _state(pulse, "journey_gate_health")
    supporting_signals = _object(pulse.get("supporting_signals"))
    launch_readiness = _string(supporting_signals.get("launch_readiness"))
    pulse_contract = _string(pulse.get("contract_name"))
    raw_pulse_contract_version = pulse.get("contract_version")
    pulse_contract_version = (
        raw_pulse_contract_version
        if type(raw_pulse_contract_version) is int
        else 0
    )
    journey_authority = _string(pulse.get("journey_gate_authority"))
    journey_scope = _string(pulse.get("journey_gate_scope"))
    receipt_scope = _string(receipt.get("readiness_scope"))
    receipt_live_status = _string(
        _object(receipt.get("live_readiness")).get("status")
    )
    release_truth_source = _string(
        pulse.get("release_truth_source")
        or supporting_signals.get("flagship_release_receipt_source")
    )
    scorecard_source = _string(pulse.get("scorecard_source"))

    if receipt_status != "pass":
        issues.append(f"flagship release receipt is {receipt_status or 'missing'}, expected pass")
    elif receipt:
        issues.extend(_flagship_receipt_pass_blockers(receipt, seed))
    if browser_status != "pass":
        issues.append(f"browser workflow proof is {browser_status or 'missing'}, expected pass")
    elif browser:
        issues.extend(
            f"browser workflow proof is internally inconsistent: {reason}"
            for reason in browser_receipt_pass_blockers(browser, seed)
        )
    expected_product = _string(seed.get("product"))
    if expected_product != "propertyquarry":
        issues.append(
            f"flagship gate seed product is {expected_product or 'missing'}, expected standalone propertyquarry"
        )
    if _string(receipt.get("product")) != expected_product:
        issues.append("flagship release receipt product does not match the current gate seed")
    if pulse_contract != "ea.weekly_product_pulse":
        issues.append(f"weekly product pulse contract is {pulse_contract or 'missing'}, expected ea.weekly_product_pulse")
    if pulse_contract_version != 2:
        issues.append(f"weekly product pulse version is {pulse_contract_version or 'missing'}, expected 2")
    if receipt_scope != "source_and_browser_proof":
        issues.append(
            f"flagship receipt readiness scope is {receipt_scope or 'missing'}, expected source_and_browser_proof"
        )
    if receipt_live_status != "not_evaluated":
        issues.append(
            f"flagship receipt live readiness is {receipt_live_status or 'missing'}, expected not_evaluated"
        )
    if release_truth_source != ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json":
        issues.append(
            "weekly product pulse release truth source is "
            f"{release_truth_source or 'missing'}, expected .codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json"
        )
    if scorecard_source and scorecard_source != ".codex-design/product/PRODUCT_HEALTH_SCORECARD.yaml":
        issues.append(
            "weekly product pulse scorecard source is "
            f"{scorecard_source}, expected .codex-design/product/PRODUCT_HEALTH_SCORECARD.yaml"
        )
    if release_health != "blocked":
        issues.append(f"weekly release_health is {release_health or 'missing'}, expected fail-closed blocked")
    if flagship_readiness != "blocked":
        issues.append(f"weekly flagship_readiness is {flagship_readiness or 'missing'}, expected fail-closed blocked")
    if _string(release_health_payload.get("candidate_state")) not in {"clear", "ready"}:
        issues.append("weekly release_health candidate_state is not clear/ready")
    if _string(flagship_readiness_payload.get("candidate_state")) not in {"clear", "ready"}:
        issues.append("weekly flagship_readiness candidate_state is not clear/ready")
    for label, payload in (
        ("release_health", release_health_payload),
        ("flagship_readiness", flagship_readiness_payload),
    ):
        if _string(payload.get("production_launch_state")) != "blocked":
            issues.append(f"weekly {label} production_launch_state must remain blocked")
        if _string(payload.get("reported_live_readiness_state")) != "not_passed":
            issues.append(f"weekly {label} reported_live_readiness_state must remain not_passed")
    if journey_health not in {"ready", "clear", "blocked", "watch", "warning"}:
        issues.append(f"weekly journey_gate_health is {journey_health or 'missing'}, expected an explicit state")
    if journey_authority != "non_authoritative_for_propertyquarry_launch":
        issues.append("weekly journey gate is not explicitly non-authoritative for PropertyQuarry launch")
    if journey_scope != "supporting_external_fleet_context":
        issues.append("weekly journey gate is not scoped to supporting external Fleet context")
    if supporting_signals.get("overall_progress_percent") is not None:
        issues.append("weekly overall_progress_percent must remain unevaluated for production launch")
    if supporting_signals.get("overall_progress_status") != "production_launch_progress_not_evaluated":
        issues.append("weekly overall progress lacks the production-launch not-evaluated status")
    if pulse.get("design_drift_count") is not None or pulse.get("public_promise_drift_count") is not None:
        issues.append("weekly unmeasured drift counts must remain null")
    if pulse.get("drift_count_status") != "not_evaluated":
        issues.append("weekly drift count status is not_evaluated")
    if pulse.get("oldest_blocker_days") is not None or pulse.get("oldest_blocker_days_status") != "not_evaluated":
        issues.append("weekly oldest blocker age must remain explicitly not_evaluated")
    if "hold production launch" not in launch_readiness.lower():
        issues.append("weekly launch_readiness does not explicitly hold production launch")
    if ".codex-design/ea/*" not in implementation_scope:
        issues.append("implementation scope no longer requires mirrored .codex-design/ea/* canon")
    if "EA product surface canon under `.codex-design/ea/*`" not in implementation_scope:
        issues.append("implementation scope no longer owns the EA product surface canon line")
    scope_heading = next(
        (line.strip() for line in implementation_scope.splitlines() if line.strip()),
        "",
    )
    if (
        expected_product == "propertyquarry"
        and scope_heading.casefold().endswith("implementation scope")
        and "propertyquarry" not in scope_heading.casefold()
    ):
        issues.append(
            "implementation scope explicitly names a different product than the current propertyquarry gate seed"
        )

    for label, snapshot in (
        ("weekly product pulse", pulse_snapshot),
        ("flagship release receipt", flagship_snapshot),
        ("browser workflow proof", browser_snapshot),
    ):
        if snapshot is None:
            continue
        try:
            snapshot.assert_unchanged()
        except ReleaseBindingError:
            issues.append(f"{label} changed during readiness verification")

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed unless PropertyQuarry source/browser candidate readiness is clear and production remains held."
    )
    parser.add_argument("--pulse", type=Path, default=DEFAULT_PULSE)
    parser.add_argument("--flagship-receipt", type=Path, default=DEFAULT_FLAGSHIP_RECEIPT)
    parser.add_argument("--browser-proof", type=Path, default=DEFAULT_BROWSER_PROOF)
    parser.add_argument("--flagship-seed", type=Path, default=DEFAULT_FLAGSHIP_SEED)
    parser.add_argument("--journey-gates", type=Path, default=DEFAULT_JOURNEY_GATES)
    parser.add_argument("--implementation-scope", type=Path, default=DEFAULT_IMPLEMENTATION_SCOPE)
    parser.add_argument("--candidate-root", type=Path, default=ROOT)
    args = parser.parse_args()

    issues = verify(
        pulse_path=args.pulse,
        flagship_receipt_path=args.flagship_receipt,
        browser_proof_path=args.browser_proof,
        journey_gates_path=args.journey_gates,
        flagship_seed_path=args.flagship_seed,
        implementation_scope_path=args.implementation_scope,
        candidate_root=args.candidate_root,
    )
    if issues:
        print(json.dumps({"status": "blocked", "issues": issues}, indent=2))
        return 1
    print(
        json.dumps(
            {
                "status": "pass",
                "message": (
                    "PropertyQuarry source/browser candidate readiness is clear; "
                    "production remains blocked pending protected live evidence."
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
