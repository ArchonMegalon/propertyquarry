#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

if __package__:
    from scripts.propertyquarry_global_governance_attestation import (
        INCIDENT_SUPPORT_GATE_ID,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot
else:
    from propertyquarry_global_governance_attestation import (  # type: ignore[no-redef]
        INCIDENT_SUPPORT_GATE_ID,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    from propertyquarry_strict_json import load_strict_json_object_snapshot


CONTRACT_SCHEMA = "propertyquarry.incident_support.v1"
LIVE_RECEIPT_SCHEMA = "propertyquarry.incident_support_live_receipt.v1"
GATE_SCHEMA = "propertyquarry.incident_support_gate.v1"
DEFAULT_CONTRACT = Path("config/monitoring/propertyquarry_incident_support.v1.json")
REQUIRED_SEVERITIES = ("SEV0", "SEV1", "SEV2", "SEV3")
REQUIRED_ROLES = (
    "incident_commander",
    "operations_lead",
    "communications_lead",
    "customer_support_lead",
    "security_privacy_lead",
)
REQUIRED_ENDPOINTS = ("paging", "status_page", "support_case_system", "security_intake")
REQUIRED_DRILLS = (
    "alert_delivery_and_acknowledgement",
    "customer_status_update",
    "support_case_round_trip",
    "privacy_breach_tabletop",
    "rollback_recovery_coordination",
)
REQUIRED_APPROVALS = ("incident_owner", "support_owner", "security_owner", "privacy_legal_owner")
REQUIRED_MARKETS = {
    "AT": {"timezone": "Europe/Vienna", "required_languages": {"de-AT", "en"}},
    "DE": {"timezone": "Europe/Berlin", "required_languages": {"de-DE", "en"}},
    "CR": {"timezone": "America/Costa_Rica", "required_languages": {"es-CR", "en"}},
}
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
OPAQUE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{5,159}")
PLACEHOLDER_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:placeholder|example|unconfigured|unassigned|unknown|todo|tbd|fake|mock|demo|sample|test|invalid|changeme|dummy|pending|localhost)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def _load_json(path: Path) -> dict[str, Any]:
    payload, _raw, _digest = load_strict_json_object_snapshot(
        path,
        field="incident support artifact",
    )
    return payload


def _load_json_with_sha256(path: Path) -> tuple[dict[str, Any], str]:
    payload, _raw, digest = load_strict_json_object_snapshot(
        path,
        field="incident support artifact",
    )
    return payload, digest


def _timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _opaque_ref(value: object) -> bool:
    raw = str(value or "").strip()
    return bool(OPAQUE_REF_RE.fullmatch(raw)) and PLACEHOLDER_RE.search(raw) is None


def _exact_release_sha(value: object) -> bool:
    raw = str(value or "").strip().lower()
    return bool(SHA_RE.fullmatch(raw) and len(set(raw)) >= 4)


def _exact_digest(value: object) -> bool:
    raw = str(value or "").strip().lower()
    payload = raw.removeprefix("sha256:")
    return bool(DIGEST_RE.fullmatch(raw) and len(set(payload)) >= 4)


def _attested_payload_digest(receipt: Mapping[str, Any]) -> str:
    payload = dict(receipt)
    payload.pop("attestation_verification", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _https_endpoint(value: object) -> bool:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        and not parsed.hostname.endswith((".localhost", ".test", ".example", ".invalid"))
        and "unconfigured" not in raw.lower()
    )


def _evidence_record(
    value: object,
    *,
    now: datetime,
    max_age_hours: float,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    observed_at = _timestamp(value.get("observed_at"))
    if observed_at is None:
        return False
    age_seconds = (now - observed_at).total_seconds()
    return (
        -300 <= age_seconds <= max_age_hours * 3600
        and _exact_digest(value.get("evidence_digest"))
        and _opaque_ref(value.get("workflow_ref"))
        and str(value.get("status") or "").strip().lower() == "pass"
    )


def validate_source_contract(contract: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if contract.get("schema") != CONTRACT_SCHEMA or contract.get("service") != "propertyquarry":
        blockers.append("incident/support source contract has the wrong schema or service")
    if contract.get("source_contract_status") != "defined":
        blockers.append("incident/support source contract is not defined")
    severity = contract.get("severity_policy")
    if not isinstance(severity, Mapping) or list(severity) != list(REQUIRED_SEVERITIES):
        blockers.append("incident/support severity policy is incomplete or out of order")
    else:
        for key in REQUIRED_SEVERITIES:
            row = severity.get(key)
            required = (
                "definition",
                "acknowledge_minutes_max",
                "incident_commander_minutes_max",
                "customer_update_minutes_max",
                "update_interval_minutes_max",
            )
            if not isinstance(row, Mapping) or any(not row.get(field) for field in required):
                blockers.append(f"incident/support severity {key} is incomplete")
    for key, expected in (
        ("required_roles", REQUIRED_ROLES),
        ("required_endpoints", REQUIRED_ENDPOINTS),
        ("required_drills", REQUIRED_DRILLS),
        ("required_approvals", REQUIRED_APPROVALS),
    ):
        if contract.get(key) != list(expected):
            blockers.append(f"incident/support {key} is incomplete or out of order")
    privacy = contract.get("privacy_security")
    if not isinstance(privacy, Mapping) or privacy.get("internal_breach_assessment_hours_max") != 24:
        blockers.append("incident/support privacy breach assessment clock is missing")
    if not isinstance(privacy, Mapping) or privacy.get("regulatory_notification_clock_hours") != 72:
        blockers.append("incident/support regulatory notification clock is missing")
    support = contract.get("support_policy")
    if not isinstance(support, Mapping) or support.get("launch_markets_require_staffed_windows") is not True:
        blockers.append("incident/support launch-market staffing rule is missing")
    market_rows = contract.get("required_launch_markets")
    market_index = {
        str(row.get("country_code") or "").strip().upper(): row
        for row in market_rows or []
        if isinstance(row, Mapping)
    } if isinstance(market_rows, list) else {}
    if len(market_index) != len(market_rows or []) or set(market_index) != set(REQUIRED_MARKETS):
        blockers.append("incident/support required launch markets must contain exactly AT, DE, and CR")
    for country_code, expected in REQUIRED_MARKETS.items():
        row = market_index.get(country_code, {})
        if row.get("timezone") != expected["timezone"]:
            blockers.append(f"incident/support launch market {country_code} timezone changed")
        if set(row.get("required_languages") or []) != expected["required_languages"]:
            blockers.append(f"incident/support launch market {country_code} required languages changed")
    evidence_policy = contract.get("live_evidence_policy")
    if not isinstance(evidence_policy, Mapping):
        blockers.append("incident/support live evidence policy is missing")
    else:
        maximum_age = evidence_policy.get("maximum_age_hours")
        if not isinstance(maximum_age, (int, float)) or isinstance(maximum_age, bool) or not math.isfinite(float(maximum_age)) or not 0 < float(maximum_age) <= 24:
            blockers.append("incident/support maximum live evidence age must be within 24 hours")
        if evidence_policy.get("independent_attestation_authority") != "independent_release_controller":
            blockers.append("incident/support independent attestation authority changed")
        for field in (
            "require_exact_release_identity",
            "require_attested_payload_digest",
            "require_workflow_references",
        ):
            if evidence_policy.get(field) is not True:
                blockers.append(f"incident/support live evidence policy {field} must be true")
    return list(dict.fromkeys(blockers))


def build_gate(
    *,
    contract_path: Path,
    live_receipt_path: Path | None = None,
    expected_release_sha: str = "",
    expected_image_digest: str = "",
    required_markets: tuple[str, ...] = (),
    max_age_hours: float = 24.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    contract, contract_sha256 = _load_json_with_sha256(contract_path)
    blockers = validate_source_contract(contract)
    contract_digest = f"sha256:{contract_sha256}"
    live: dict[str, Any] | None = None
    age_seconds: float | None = None

    expected_release_sha = str(expected_release_sha or "").strip().lower()
    expected_image_digest = str(expected_image_digest or "").strip().lower()
    policy_value = (
        (contract.get("live_evidence_policy") or {}).get("maximum_age_hours", 24)
        if isinstance(contract.get("live_evidence_policy"), Mapping)
        else 24
    )
    policy_age = (
        float(policy_value)
        if isinstance(policy_value, (int, float))
        and not isinstance(policy_value, bool)
        and math.isfinite(float(policy_value))
        and 0 < float(policy_value) <= 24
        else 24.0
    )
    if isinstance(max_age_hours, bool) or not isinstance(max_age_hours, (int, float)) or not math.isfinite(float(max_age_hours)) or float(max_age_hours) <= 0:
        blockers.append("maximum evidence age must be finite and greater than zero")
        max_age_hours = policy_age
    effective_age_hours = min(policy_age, float(max_age_hours))
    requested_markets = tuple(
        dict.fromkeys(
            str(item or "").strip().upper()
            for item in required_markets
            if str(item or "").strip()
        )
    )
    normalized_markets = tuple(REQUIRED_MARKETS)
    if requested_markets and requested_markets != normalized_markets:
        blockers.append("required markets must be the exact ordered AT, DE, and CR launch envelope")
    if not _exact_release_sha(expected_release_sha):
        blockers.append("expected exact non-placeholder 40-character release SHA is required")
    if not _exact_digest(expected_image_digest):
        blockers.append("expected immutable non-placeholder sha256 image digest is required")

    if live_receipt_path is None:
        blockers.append("fresh independently attested live incident/support receipt is required")
    else:
        try:
            live = _load_json(live_receipt_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"live incident/support receipt is unreadable: {type(exc).__name__}")
            live = None

    if live is not None:
        if live.get("schema") != LIVE_RECEIPT_SCHEMA:
            blockers.append("live incident/support receipt has the wrong schema")
        if live.get("profile") != "launch" or live.get("claim_scope") != "core":
            blockers.append("live incident/support receipt is not launch-tier Core evidence")
        generated_at = _timestamp(live.get("generated_at"))
        if generated_at is None:
            blockers.append("live incident/support receipt has no timezone-aware generated_at")
        else:
            age_seconds = (now - generated_at).total_seconds()
            if age_seconds < -300 or age_seconds > effective_age_hours * 3600:
                blockers.append("live incident/support receipt is stale or future-dated")
        if str(live.get("contract_sha256") or "").strip().lower() != contract_digest:
            blockers.append("live incident/support receipt is not bound to the current source contract")
        identity = live.get("release_identity")
        if not isinstance(identity, Mapping):
            identity = {}
        if str(identity.get("commit_sha") or "").strip().lower() != expected_release_sha:
            blockers.append("live incident/support receipt commit does not match the expected release")
        if str(identity.get("image_digest") or "").strip().lower() != expected_image_digest:
            blockers.append("live incident/support receipt image does not match the expected release")

        roles = live.get("roles")
        if not isinstance(roles, Mapping) or set(roles) != set(REQUIRED_ROLES):
            blockers.append("live incident/support receipt does not assign every required role")
        else:
            for role in REQUIRED_ROLES:
                row = roles.get(role)
                if not isinstance(row, Mapping) or not _opaque_ref(row.get("primary_operator_ref")) or not _opaque_ref(row.get("backup_operator_ref")):
                    blockers.append(f"live incident/support role {role} lacks distinct governed primary/backup refs")
                elif row.get("primary_operator_ref") == row.get("backup_operator_ref"):
                    blockers.append(f"live incident/support role {role} uses the same primary and backup")

        endpoints = live.get("endpoints")
        if not isinstance(endpoints, Mapping) or set(endpoints) != set(REQUIRED_ENDPOINTS):
            blockers.append("live incident/support receipt does not configure every endpoint class")
        else:
            for endpoint in REQUIRED_ENDPOINTS:
                if not _https_endpoint(endpoints.get(endpoint)):
                    blockers.append(f"live incident/support endpoint {endpoint} is not a safe configured HTTPS endpoint")

        market_rows = live.get("launch_market_support")
        market_index = {
            str(row.get("country_code") or "").strip().upper(): row
            for row in market_rows or []
            if isinstance(row, Mapping) and str(row.get("country_code") or "").strip()
        } if isinstance(market_rows, list) else {}
        if len(market_index) != len(market_rows or []) or set(market_index) != set(REQUIRED_MARKETS):
            blockers.append("live incident/support receipt must cover exactly AT, DE, and CR once each")
        for country_code in normalized_markets:
            row = market_index.get(country_code)
            expected_market = REQUIRED_MARKETS[country_code]
            languages = set(row.get("languages") or []) if isinstance(row, Mapping) else set()
            if (
                not isinstance(row, Mapping)
                or row.get("staffed") is not True
                or row.get("timezone") != expected_market["timezone"]
                or not str(row.get("support_window") or "").strip()
                or not expected_market["required_languages"].issubset(languages)
                or not _opaque_ref(row.get("primary_owner_ref"))
                or not _opaque_ref(row.get("backup_owner_ref"))
                or row.get("primary_owner_ref") == row.get("backup_owner_ref")
            ):
                blockers.append(f"launch market {country_code} lacks complete staffed support coverage")

        drills = live.get("drills")
        drill_index = {
            str(row.get("drill_id") or "").strip(): row
            for row in drills or []
            if isinstance(row, Mapping)
        } if isinstance(drills, list) else {}
        if len(drill_index) != len(drills or []) or set(drill_index) != set(REQUIRED_DRILLS):
            blockers.append("live incident/support receipt must contain every required drill exactly once")
        for drill_id in REQUIRED_DRILLS:
            if not _evidence_record(
                drill_index.get(drill_id),
                now=now,
                max_age_hours=effective_age_hours,
            ):
                blockers.append(f"required incident/support drill is not proved: {drill_id}")

        approvals = live.get("approvals")
        approval_index = {
            str(row.get("control") or "").strip(): row
            for row in approvals or []
            if isinstance(row, Mapping)
        } if isinstance(approvals, list) else {}
        if len(approval_index) != len(approvals or []) or set(approval_index) != set(REQUIRED_APPROVALS):
            blockers.append("live incident/support receipt must contain every required approval exactly once")
        for control in REQUIRED_APPROVALS:
            row = approval_index.get(control)
            if not _evidence_record(
                row,
                now=now,
                max_age_hours=effective_age_hours,
            ) or not _opaque_ref((row or {}).get("reviewer_ref")):
                blockers.append(f"required incident/support owner approval is not proved: {control}")

        attestation = live.get("attestation_verification")
        try:
            verify_global_governance_attestation(
                attestation if isinstance(attestation, Mapping) else {},
                expected_subject={
                    "gate_id": INCIDENT_SUPPORT_GATE_ID,
                    "receipt_contract": LIVE_RECEIPT_SCHEMA,
                    "release_commit_sha": expected_release_sha,
                    "release_image_digest": expected_image_digest,
                    "source_digests": {
                        "incident_support_contract_sha256": contract_digest,
                    },
                    "payload_sha256": _attested_payload_digest(live),
                },
                observed_at=now,
            )
        except GlobalGovernanceAttestationError:
            blockers.append("live incident/support receipt lacks independent exact-release attestation verification")

    blockers = list(dict.fromkeys(blockers))
    return {
        "schema": GATE_SCHEMA,
        "status": "blocked" if blockers else "pass",
        "generated_at": now.isoformat(),
        "source_contract": {
            "path": contract_path.as_posix(),
            "sha256": contract_digest,
            "status": "pass" if not validate_source_contract(contract) else "blocked",
        },
        "live_receipt_path": live_receipt_path.as_posix() if live_receipt_path else "",
        "release_identity": {
            "commit_sha": expected_release_sha,
            "image_digest": expected_image_digest,
        },
        "required_markets": list(normalized_markets),
        "maximum_age_hours": effective_age_hours,
        "live_receipt_age_seconds": age_seconds,
        "blockers": blockers,
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify PropertyQuarry live incident and support launch evidence.")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--live-receipt", type=Path)
    parser.add_argument("--expected-release-sha", default="")
    parser.add_argument("--expected-image-digest", default="")
    parser.add_argument("--required-market", action="append", default=[])
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-blocked", action="store_true")
    args = parser.parse_args()
    receipt = build_gate(
        contract_path=args.contract,
        live_receipt_path=args.live_receipt,
        expected_release_sha=args.expected_release_sha,
        expected_image_digest=args.expected_image_digest,
        required_markets=tuple(args.required_market),
        max_age_hours=args.max_age_hours,
    )
    if args.output:
        _atomic_write(args.output, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 1 if args.fail_on_blocked and receipt["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
