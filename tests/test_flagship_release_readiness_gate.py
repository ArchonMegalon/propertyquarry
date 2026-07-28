from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import verify_flagship_release_readiness as readiness_verifier
from scripts.propertyquarry_release_receipt_binding import committed_source_binding
from scripts.propertyquarry_release_receipt_binding import git_blob_oid_bytes
from scripts.propertyquarry_release_receipt_binding import sha256_bytes


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_flagship_release_readiness.py"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _bind_pulse_to_receipt(
    pulse_payload: dict[str, object],
    receipt_path: Path,
) -> None:
    receipt_bytes = receipt_path.read_bytes()
    provenance = pulse_payload.get("release_truth_provenance")
    assert isinstance(provenance, dict)
    provenance.update(
        {
            "present": True,
            "repo_relative_path": (
                ".codex-design/product/"
                "EA_FLAGSHIP_RELEASE_GATE.generated.json"
            ),
            "sha256": sha256_bytes(receipt_bytes),
            "size_bytes": len(receipt_bytes),
        }
    )


def _write_release_chain(
    *,
    candidate_root: Path,
    pulse_path: Path,
    pulse_payload: dict[str, object],
    receipt_path: Path,
    receipt_payload: dict[str, object],
    browser_path: Path,
    browser_payload: dict[str, object],
) -> None:
    _write_json(browser_path, browser_payload)
    browser_bytes = browser_path.read_bytes()
    receipt_payload["browser_receipt_binding"] = {
        "path": (
            ".codex-studio/published/"
            "EA_BROWSER_WORKFLOW_PROOF.generated.json"
        ),
        "sha256": sha256_bytes(browser_bytes),
        "git_blob_oid": git_blob_oid_bytes(candidate_root, browser_bytes),
    }
    _write_json(receipt_path, receipt_payload)
    _bind_pulse_to_receipt(pulse_payload, receipt_path)
    _write_json(pulse_path, pulse_payload)


@pytest.mark.parametrize(
    "payload",
    (
        b'{"status":"blocked","status":"pass"}',
        b'{"status":NaN}',
        b'["pass"]',
        b'{"status":"pass"}\xff',
    ),
)
def test_flagship_readiness_json_loader_rejects_ambiguous_or_noncanonical_input(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(payload)

    assert readiness_verifier._json(path) == {}


def test_flagship_readiness_loaders_reject_symlinked_evidence(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"status":"pass"}\n', encoding="utf-8")
    link = tmp_path / "artifact.json"
    link.symlink_to(target.name)

    assert readiness_verifier._json(link) == {}
    assert readiness_verifier._text(link) == ""


def test_flagship_readiness_governs_nested_binding_type_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_path = tmp_path / "seed.json"

    def malformed_binding(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TypeError("malformed nested evidence cases")

    monkeypatch.setattr(
        readiness_verifier,
        "build_source_binding",
        malformed_binding,
    )
    issues = readiness_verifier._source_binding_issues(
        candidate_root=tmp_path,
        seed_path=seed_path,
        seed={"browser_workflow_proof": {"evidence_sources": []}},
        flagship_receipt={"source_binding": {}},
        browser_proof={"source_binding": {}},
    )

    assert issues == [
        "current candidate source identity is invalid: malformed nested evidence cases"
    ]


def _current_candidate_binding() -> dict[str, object]:
    seed = json.loads(
        (ROOT / ".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json").read_text(encoding="utf-8")
    )
    return committed_source_binding(
        ROOT,
        seed_path=Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json"),
        evidence_sources=seed["browser_workflow_proof"]["evidence_sources"],
    )


def _passing_browser_proof() -> dict[str, object]:
    proof = json.loads(
        (ROOT / ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json").read_text(
            encoding="utf-8"
        )
    )
    seed = json.loads(
        (ROOT / ".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json").read_text(encoding="utf-8")
    )
    proof["status"] = "pass"
    proof["blocking_reasons"] = []
    proof["current_limitations"] = []
    proof["release_claim_summary"] = seed["release_claim"]["summary"]
    proof["expected_browser_signals"] = seed["browser_workflow_proof"]["expected_browser_signals"]
    proof["source_binding"] = _current_candidate_binding()
    for source in seed["browser_workflow_proof"]["evidence_sources"]:
        lane_key = "real_browser_e2e_proof" if "/e2e/" in source["file"] else "source_backed_journey_proof"
        lane = proof[lane_key]
        cases = list(source["cases"])
        lane["status"] = "pass"
        lane["cases"] = cases
        lane["required_case_count"] = len(cases)
        lane["selected_count"] = len(cases)
        lane["executed_count"] = len(cases)
        lane["exit_code"] = 0
        lane["limitations"] = []
        lane["outcome_counts"] = {
            "passed": len(cases),
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
        }

    source_binding = proof.get("source_binding")
    runtime_commit_sha = (
        str(source_binding.get("code_commit") or "") if isinstance(source_binding, dict) else ""
    )
    matrix_seed = seed["journey_evidence_matrix"]
    proof["journey_evidence_matrix"] = {
        "version": matrix_seed["version"],
        "status": "pass",
        "readiness_scope": matrix_seed["readiness_scope"],
        "runtime_commit_sha": runtime_commit_sha,
        "required_journey_ids": list(matrix_seed["required_journey_ids"]),
        "rows": [
            {
                "journey_id": row["journey_id"],
                "label": row["label"],
                "proof_status": "pass",
                "evidence_sources": [
                    {
                        "file": source["file"],
                        "cases": list(source["cases"]),
                        "lane_status": "pass",
                    }
                    for source in row["evidence_sources"]
                ],
                "live_requirement": dict(row["live_requirement"]),
                "blocking_reasons": [],
            }
            for row in matrix_seed["rows"]
        ],
    }
    return proof


def _passing_flagship_receipt() -> dict[str, object]:
    receipt = json.loads(
        (ROOT / ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json").read_text(
            encoding="utf-8"
        )
    )
    receipt["status"] = "pass"
    receipt["blocking_reasons"] = []
    receipt["current_limitations"] = []
    receipt["source_binding"] = _current_candidate_binding()
    return receipt


def _candidate_bound_receipts(
    source_binding: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    receipt = _passing_flagship_receipt()
    browser = _passing_browser_proof()
    receipt["source_binding"] = source_binding
    browser["source_binding"] = source_binding
    matrix = browser.get("journey_evidence_matrix")
    if isinstance(matrix, dict):
        matrix["runtime_commit_sha"] = source_binding["code_commit"]
    return receipt, browser


def _passing_pulse(*, journey_path: Path | None = None) -> dict[str, object]:
    pulse = json.loads(
        (ROOT / ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json").read_text(
            encoding="utf-8"
        )
    )
    pulse["release_health"]["candidate_state"] = "clear"
    pulse["release_health"]["flagship_receipt_status"] = "pass"
    pulse["flagship_readiness"]["candidate_state"] = "clear"
    if journey_path is not None:
        source = journey_path.as_posix()
        pulse["journey_gate_source"] = source
        pulse["supporting_signals"]["journey_gate_source"] = source
    return pulse


def test_flagship_release_readiness_gate_repository_defaults_share_product_identity() -> None:
    seed = json.loads(
        (ROOT / ".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json").read_text(encoding="utf-8")
    )
    browser = json.loads(
        (ROOT / ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json").read_text(
            encoding="utf-8"
        )
    )
    receipt = json.loads(
        (ROOT / ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json").read_text(
            encoding="utf-8"
        )
    )
    pulse = json.loads(
        (ROOT / ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json").read_text(
            encoding="utf-8"
        )
    )

    expected_product = seed["product"]
    assert expected_product == "propertyquarry"
    implementation_scope = (
        ROOT / ".codex-design/repo/IMPLEMENTATION_SCOPE.md"
    ).read_text(encoding="utf-8")
    assert implementation_scope.splitlines()[0] == "# PropertyQuarry implementation scope"
    assert browser["product"] == expected_product
    assert browser["proof_target"] == seed["browser_workflow_proof"]["proof_target"]
    assert browser["release_claim_summary"] == seed["release_claim"]["summary"]
    assert browser["expected_browser_signals"] == seed["browser_workflow_proof"]["expected_browser_signals"]
    assert receipt["product"] == expected_product
    assert "Executive Assistant" not in json.dumps(pulse)


def test_flagship_release_readiness_gate_keeps_external_fleet_journey_non_authoritative(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, candidate_binding = _initialize_candidate_repository(candidate_root)
    receipt_payload, browser_payload = _candidate_bound_receipts(candidate_binding)
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    pulse_payload = _passing_pulse()
    pulse_payload["journey_gate_health"]["state"] = "blocked"
    pulse_payload["journey_gate_health"]["blocked_count"] = 1
    _write_release_chain(
        candidate_root=candidate_root,
        pulse_path=pulse,
        pulse_payload=pulse_payload,
        receipt_path=receipt,
        receipt_payload=receipt_payload,
        browser_path=browser,
        browser_payload=browser_payload,
    )
    _write_json(journey, {"summary": {"overall_state": "blocked", "blocked_count": 1}})
    scope.write_text("EA product surface canon under `.codex-design/ea/*`\nmirrored `.codex-design/ea/*`\n", encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--flagship-seed",
            str(candidate_seed),
            "--candidate-root",
            str(candidate_root),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert '"status": "pass"' in result.stdout


def test_flagship_release_readiness_gate_passes_when_receipts_and_journeys_are_clear(tmp_path: Path) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, candidate_binding = _initialize_candidate_repository(candidate_root)
    receipt_payload, browser_payload = _candidate_bound_receipts(candidate_binding)
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    _write_release_chain(
        candidate_root=candidate_root,
        pulse_path=pulse,
        pulse_payload=_passing_pulse(),
        receipt_path=receipt,
        receipt_payload=receipt_payload,
        browser_path=browser,
        browser_payload=browser_payload,
    )
    _write_json(journey, {"summary": {"overall_state": "ready", "blocked_count": 0}})
    scope.write_text("EA product surface canon under `.codex-design/ea/*`\nmirrored `.codex-design/ea/*`\n", encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--flagship-seed",
            str(candidate_seed),
            "--candidate-root",
            str(candidate_root),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert '"status": "pass"' in result.stdout


def test_flagship_readiness_rejects_stale_browser_receipt_binding(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    _candidate_seed, candidate_binding = _initialize_candidate_repository(
        candidate_root
    )
    receipt_payload, browser_payload = _candidate_bound_receipts(
        candidate_binding
    )
    pulse_path = tmp_path / "pulse.json"
    receipt_path = tmp_path / "receipt.json"
    browser_path = tmp_path / "browser.json"
    _write_release_chain(
        candidate_root=candidate_root,
        pulse_path=pulse_path,
        pulse_payload=_passing_pulse(),
        receipt_path=receipt_path,
        receipt_payload=receipt_payload,
        browser_path=browser_path,
        browser_payload=browser_payload,
    )
    pulse, _pulse_snapshot = readiness_verifier._json_snapshot(pulse_path)
    receipt, flagship_snapshot = readiness_verifier._json_snapshot(receipt_path)
    _browser, browser_snapshot = readiness_verifier._json_snapshot(browser_path)
    binding = receipt.get("browser_receipt_binding")
    assert isinstance(binding, dict)
    binding["sha256"] = "0" * 64

    issues = readiness_verifier._receipt_chain_issues(
        candidate_root=candidate_root,
        pulse=pulse,
        flagship_receipt=receipt,
        browser_snapshot=browser_snapshot,
        flagship_snapshot=flagship_snapshot,
    )

    assert (
        "flagship browser_receipt_binding does not match the canonical "
        "browser proof bytes"
        in issues
    )


def test_flagship_readiness_rejects_stale_weekly_flagship_provenance(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    _candidate_seed, candidate_binding = _initialize_candidate_repository(
        candidate_root
    )
    receipt_payload, browser_payload = _candidate_bound_receipts(
        candidate_binding
    )
    pulse_path = tmp_path / "pulse.json"
    receipt_path = tmp_path / "receipt.json"
    browser_path = tmp_path / "browser.json"
    _write_release_chain(
        candidate_root=candidate_root,
        pulse_path=pulse_path,
        pulse_payload=_passing_pulse(),
        receipt_path=receipt_path,
        receipt_payload=receipt_payload,
        browser_path=browser_path,
        browser_payload=browser_payload,
    )
    pulse, _pulse_snapshot = readiness_verifier._json_snapshot(pulse_path)
    receipt, flagship_snapshot = readiness_verifier._json_snapshot(receipt_path)
    _browser, browser_snapshot = readiness_verifier._json_snapshot(browser_path)
    provenance = pulse.get("release_truth_provenance")
    assert isinstance(provenance, dict)
    provenance["sha256"] = "0" * 64
    provenance["size_bytes"] = 0

    issues = readiness_verifier._receipt_chain_issues(
        candidate_root=candidate_root,
        pulse=pulse,
        flagship_receipt=receipt,
        browser_snapshot=browser_snapshot,
        flagship_snapshot=flagship_snapshot,
    )

    assert (
        "weekly release_truth_provenance sha256 does not match the canonical "
        "flagship receipt bytes"
        in issues
    )
    assert (
        "weekly release_truth_provenance size_bytes does not match the "
        "canonical flagship receipt bytes"
        in issues
    )


def test_flagship_release_readiness_gate_rejects_false_green_all_skipped_browser_proof(tmp_path: Path) -> None:
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    _write_json(pulse, _passing_pulse())
    _write_json(receipt, _passing_flagship_receipt())
    false_green_browser = _passing_browser_proof()
    false_green_browser["real_browser_e2e_proof"] = {
        "status": "pass",
        "test_file": "tests/e2e/test_propertyquarry_greenfield_browser.py",
        "cases": [
            "test_propertyquarry_greenfield_workspace_in_real_browser",
            "test_propertyquarry_greenfield_workspace_is_mobile_usable",
        ],
        "exit_code": 0,
        "output_excerpt": ["1 skipped, 20 deselected in 0.79s"],
        "limitations": ["real browser E2E did not run to completion"],
    }
    _write_json(browser, false_green_browser)
    _write_json(journey, {"summary": {"overall_state": "ready", "blocked_count": 0}})
    scope.write_text("EA product surface canon under `.codex-design/ea/*`\nmirrored `.codex-design/ea/*`\n", encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 1
    assert "browser workflow proof is internally inconsistent" in result.stdout
    assert "published pass lacks completed real browser E2E proof" in result.stdout


def test_flagship_release_readiness_gate_accepts_committed_journey_snapshot_when_external_receipt_is_absent(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, candidate_binding = _initialize_candidate_repository(candidate_root)
    receipt_payload, browser_payload = _candidate_bound_receipts(candidate_binding)
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "missing" / "journey.json"
    scope = tmp_path / "scope.md"
    _write_release_chain(
        candidate_root=candidate_root,
        pulse_path=pulse,
        pulse_payload=_passing_pulse(journey_path=journey),
        receipt_path=receipt,
        receipt_payload=receipt_payload,
        browser_path=browser,
        browser_payload=browser_payload,
    )
    scope.write_text("EA product surface canon under `.codex-design/ea/*`\nmirrored `.codex-design/ea/*`\n", encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--flagship-seed",
            str(candidate_seed),
            "--candidate-root",
            str(candidate_root),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert '"status": "pass"' in result.stdout


def test_flagship_release_readiness_gate_fails_when_external_receipt_and_snapshot_are_absent(tmp_path: Path) -> None:
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "missing" / "journey.json"
    scope = tmp_path / "scope.md"
    pulse_payload = _passing_pulse(journey_path=journey)
    pulse_payload.pop("journey_gate_health", None)
    _write_json(pulse, pulse_payload)
    _write_json(receipt, _passing_flagship_receipt())
    _write_json(browser, _passing_browser_proof())
    scope.write_text("EA product surface canon under `.codex-design/ea/*`\nmirrored `.codex-design/ea/*`\n", encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 1
    assert "journey gates summary missing or invalid" in result.stdout


def test_flagship_release_readiness_gate_rejects_unsourced_journey_snapshot_when_external_receipt_is_absent(
    tmp_path: Path,
) -> None:
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "missing" / "journey.json"
    scope = tmp_path / "scope.md"
    _write_json(pulse, _passing_pulse())
    _write_json(receipt, _passing_flagship_receipt())
    _write_json(browser, _passing_browser_proof())
    scope.write_text("EA product surface canon under `.codex-design/ea/*`\nmirrored `.codex-design/ea/*`\n", encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 1
    assert "journey gates summary missing or invalid" in result.stdout


def test_flagship_release_readiness_gate_rejects_chummer_pulse_and_wrong_product_scope(tmp_path: Path) -> None:
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    pulse_payload = _passing_pulse()
    pulse_payload["contract_name"] = "chummer.weekly_product_pulse"
    pulse_payload["scorecard_source"] = "products/chummer/PRODUCT_HEALTH_SCORECARD.yaml"
    pulse_payload["release_truth_source"] = ""
    _write_json(pulse, pulse_payload)
    _write_json(receipt, _passing_flagship_receipt())
    _write_json(browser, _passing_browser_proof())
    _write_json(journey, {"summary": {"overall_state": "ready", "blocked_count": 0}})
    scope.write_text(
        "# Executive Assistant implementation scope\n\nmirrored `.codex-design/product/*`\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 1
    assert "expected ea.weekly_product_pulse" in result.stdout
    assert "products/chummer/PRODUCT_HEALTH_SCORECARD.yaml" in result.stdout
    assert "implementation scope no longer requires mirrored .codex-design/ea/* canon" in result.stdout
    assert "implementation scope explicitly names a different product" in result.stdout


def _initialize_candidate_repository(root: Path) -> tuple[Path, dict[str, object]]:
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Readiness Test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "readiness@example.test"],
        cwd=root,
        check=True,
    )
    seed_path = Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json")
    seed = json.loads(
        (ROOT / seed_path).read_text(encoding="utf-8")
    )
    _write_json(root / seed_path, seed)
    for source in seed["browser_workflow_proof"]["evidence_sources"]:
        path = root / str(source["file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# candidate evidence source\n", encoding="utf-8")
    (root / "app.py").write_text("CANDIDATE = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "candidate"], cwd=root, check=True)
    binding = committed_source_binding(
        root,
        seed_path=seed_path,
        evidence_sources=seed["browser_workflow_proof"]["evidence_sources"],
    )
    return root / seed_path, binding


def test_flagship_release_readiness_rejects_symlinked_candidate_seed(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, _binding = _initialize_candidate_repository(candidate_root)
    seed = json.loads(candidate_seed.read_text(encoding="utf-8"))
    alternate_seed = candidate_seed.with_name("ALTERNATE_FLAGSHIP_RELEASE_GATE.json")
    candidate_seed.rename(alternate_seed)
    candidate_seed.symlink_to(alternate_seed.name)
    subprocess.run(["git", "add", "-A"], cwd=candidate_root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "replace canonical seed with symlink"],
        cwd=candidate_root,
        check=True,
    )
    alternate_binding = committed_source_binding(
        candidate_root,
        seed_path=alternate_seed.relative_to(candidate_root),
        evidence_sources=seed["browser_workflow_proof"]["evidence_sources"],
    )

    issues = readiness_verifier._source_binding_issues(
        candidate_root=candidate_root,
        seed_path=candidate_seed,
        seed=seed,
        flagship_receipt={"source_binding": alternate_binding},
        browser_proof={"source_binding": alternate_binding},
    )

    assert any("current candidate source identity is invalid" in issue for issue in issues)


def test_flagship_release_readiness_compares_source_binding_types_exactly(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, candidate_binding = _initialize_candidate_repository(candidate_root)
    seed = json.loads(candidate_seed.read_text(encoding="utf-8"))
    ambiguous_binding = dict(candidate_binding)
    ambiguous_binding["version"] = True

    issues = readiness_verifier._source_binding_issues(
        candidate_root=candidate_root,
        seed_path=candidate_seed,
        seed=seed,
        flagship_receipt={"source_binding": ambiguous_binding},
        browser_proof={"source_binding": ambiguous_binding},
    )

    assert any("source_binding does not match" in issue for issue in issues)


def test_flagship_release_readiness_gate_requires_both_candidate_source_bindings(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, _binding = _initialize_candidate_repository(candidate_root)
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    receipt_payload = _passing_flagship_receipt()
    browser_payload = _passing_browser_proof()
    receipt_payload["source_binding"] = None
    browser_payload["source_binding"] = None
    _write_json(pulse, _passing_pulse())
    _write_json(receipt, receipt_payload)
    _write_json(browser, browser_payload)
    _write_json(journey, {"summary": {"overall_state": "ready", "blocked_count": 0}})
    scope.write_text(
        "EA product surface canon under `.codex-design/ea/*`\n"
        "mirrored `.codex-design/ea/*`\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--flagship-seed",
            str(candidate_seed),
            "--candidate-root",
            str(candidate_root),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 1
    assert "flagship release receipt source_binding is missing or invalid" in result.stdout
    assert "browser workflow proof source_binding is missing or invalid" in result.stdout


def test_flagship_release_readiness_gate_rejects_bindings_for_another_candidate(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, candidate_binding = _initialize_candidate_repository(candidate_root)
    stale_binding = dict(candidate_binding)
    stale_binding["code_commit"] = "f" * 40
    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    receipt_payload = _passing_flagship_receipt()
    browser_payload = _passing_browser_proof()
    receipt_payload["source_binding"] = stale_binding
    browser_payload["source_binding"] = stale_binding
    _write_json(pulse, _passing_pulse())
    _write_json(receipt, receipt_payload)
    _write_json(browser, browser_payload)
    _write_json(journey, {"summary": {"overall_state": "ready", "blocked_count": 0}})
    scope.write_text(
        "EA product surface canon under `.codex-design/ea/*`\n"
        "mirrored `.codex-design/ea/*`\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--flagship-seed",
            str(candidate_seed),
            "--candidate-root",
            str(candidate_root),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 1
    assert "flagship release receipt source_binding does not match" in result.stdout
    assert "browser workflow proof source_binding does not match" in result.stdout


@pytest.mark.parametrize("dirty_kind", ("tracked", "untracked"))
def test_flagship_release_readiness_gate_rejects_dirty_candidate_source(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, candidate_binding = _initialize_candidate_repository(candidate_root)
    receipt_payload, browser_payload = _candidate_bound_receipts(candidate_binding)
    dirty_path = (
        candidate_root / "app.py"
        if dirty_kind == "tracked"
        else candidate_root / "untracked_runtime.py"
    )
    dirty_path.write_text("DIRTY_CANDIDATE = True\n", encoding="utf-8")

    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    _write_json(pulse, _passing_pulse())
    _write_json(receipt, receipt_payload)
    _write_json(browser, browser_payload)
    _write_json(journey, {"summary": {"overall_state": "ready", "blocked_count": 0}})
    scope.write_text(
        "EA product surface canon under `.codex-design/ea/*`\n"
        "mirrored `.codex-design/ea/*`\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--pulse",
            str(pulse),
            "--flagship-receipt",
            str(receipt),
            "--browser-proof",
            str(browser),
            "--flagship-seed",
            str(candidate_seed),
            "--candidate-root",
            str(candidate_root),
            "--journey-gates",
            str(journey),
            "--implementation-scope",
            str(scope),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 1
    assert "current candidate source identity is invalid" in result.stdout
    assert dirty_path.name in result.stdout


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    (
        ("release_health_object", "weekly release_health"),
        ("contract_version", "weekly product pulse version"),
        ("live_readiness_object", "flagship receipt live readiness"),
        ("receipt_blockers", "flagship release receipt still reports blockers"),
        (
            "receipt_generator",
            "flagship release receipt was not produced by the governed materializer",
        ),
        ("journey_summary_count", "journey gates summary missing or invalid"),
    ),
)
def test_flagship_release_readiness_fails_closed_on_malformed_or_inconsistent_evidence(
    tmp_path: Path,
    mutation: str,
    expected_issue: str,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_seed, candidate_binding = _initialize_candidate_repository(candidate_root)
    receipt_payload, browser_payload = _candidate_bound_receipts(candidate_binding)
    pulse_payload = _passing_pulse()
    journey_payload: dict[str, object] = {
        "summary": {"overall_state": "ready", "blocked_count": 0}
    }

    if mutation == "release_health_object":
        pulse_payload["release_health"] = "blocked"
    elif mutation == "contract_version":
        pulse_payload["contract_version"] = "two"
    elif mutation == "live_readiness_object":
        receipt_payload["live_readiness"] = "not_evaluated"
    elif mutation == "receipt_blockers":
        receipt_payload["blocking_reasons"] = ["still blocked"]
    elif mutation == "receipt_generator":
        receipt_payload["generated_by"] = "scripts/forged_release_receipt.py"
    else:
        journey_payload["summary"] = {
            "overall_state": "ready",
            "blocked_count": "0",
        }

    pulse = tmp_path / "pulse.json"
    receipt = tmp_path / "receipt.json"
    browser = tmp_path / "browser.json"
    journey = tmp_path / "journey.json"
    scope = tmp_path / "scope.md"
    _write_json(pulse, pulse_payload)
    _write_json(receipt, receipt_payload)
    _write_json(browser, browser_payload)
    _write_json(journey, journey_payload)
    scope.write_text(
        "EA product surface canon under `.codex-design/ea/*`\n"
        "mirrored `.codex-design/ea/*`\n",
        encoding="utf-8",
    )

    issues = readiness_verifier.verify(
        pulse_path=pulse,
        flagship_receipt_path=receipt,
        browser_proof_path=browser,
        flagship_seed_path=candidate_seed,
        candidate_root=candidate_root,
        journey_gates_path=journey,
        implementation_scope_path=scope,
        required_contract_paths=(),
    )

    assert any(expected_issue in issue for issue in issues)
