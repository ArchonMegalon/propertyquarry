from __future__ import annotations

import errno
import importlib.util
import json
import subprocess
from pathlib import Path

import yaml
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "materialize_weekly_product_pulse.py"
PULSE_PATH = Path(".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json")
SCORECARD_PATH = Path(".codex-design/product/PRODUCT_HEALTH_SCORECARD.yaml")
FLAGSHIP_RECEIPT_PATH = Path(".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json")
JOURNEY_GATES_PATH = Path("/tmp/ea-weekly-pulse-journey-gates.generated.json")


def _load_materializer_module():
    spec = importlib.util.spec_from_file_location("weekly_product_pulse_materializer", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_weekly_product_pulse_json_loader_rejects_ambiguous_input(
    tmp_path: Path,
) -> None:
    module = _load_materializer_module()
    path = tmp_path / "receipt.json"
    path.write_text(
        '{"status":"blocked","status":"pass"}\n',
        encoding="utf-8",
    )

    assert module._load_json(path) is None


def _seed_truth_sources(root: Path) -> None:
    (root / SCORECARD_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / FLAGSHIP_RECEIPT_PATH).parent.mkdir(parents=True, exist_ok=True)

    scorecard = {
        "product": "propertyquarry",
        "version": 1,
        "cadence": {"review": "weekly", "snapshot_owner": "product_governor", "publication": "internal_canon_first"},
        "scorecards": [
            {
                "id": "release_health",
                "metrics": [
                    {"name": "promoted_regressions_open", "target": 0, "source": "weekly pulse"},
                ],
            },
            {
                "id": "flagship_readiness",
                "metrics": [
                    {"name": "flagship_acceptance_surfaces_failing", "target": 0, "source": "receipt"},
                ],
            },
        ],
    }
    (root / SCORECARD_PATH).write_text(yaml.safe_dump(scorecard, sort_keys=False), encoding="utf-8")

    receipt = {
        "product": "PropertyQuarry",
        "surface": "flagship_release_control",
        "version": 1,
        "truth_plane": {
            "source": ".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md",
            "legacy_history": "MILESTONE.json",
        },
        "release_claim": {
            "summary": "EA can only claim flagship-grade release truth when the browser workflow proof and release asset verification agree with this gate seed.",
            "required_conditions": [],
        },
        "browser_workflow_proof": {
            "evidence_sources": [],
            "published_receipt": ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
            "published_receipt_present": False,
        },
        "readiness_scope": "source_and_browser_proof",
        "live_readiness": {
            "status": "not_evaluated",
            "authority": "_completion/property_gold_status/release-gate.json",
            "required_profile": "launch",
        },
        "status": "preview_only",
        "current_limitations": ["no published browser execution receipt is attached yet"],
    }
    (root / FLAGSHIP_RECEIPT_PATH).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    journey_gates = {
        "contract_name": "fleet.journey_gates",
        "contract_version": 1,
        "generated_at": "2026-04-10T15:00:38Z",
        "summary": {
            "overall_state": "blocked",
            "total_journey_count": 6,
            "ready_count": 3,
            "warning_count": 0,
            "blocked_count": 3,
            "recommended_action": "Resolve the blocking golden-journey gaps before widening publish claims.",
        },
        "journeys": [],
    }
    Path(JOURNEY_GATES_PATH).write_text(json.dumps(journey_gates, indent=2) + "\n", encoding="utf-8")


def test_weekly_product_pulse_materializer_uses_current_receipt_product_label(tmp_path: Path) -> None:
    _seed_truth_sources(tmp_path)

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--scorecard",
            SCORECARD_PATH.as_posix(),
            "--journey-gates",
            str(JOURNEY_GATES_PATH),
            "--flagship-receipt",
            FLAGSHIP_RECEIPT_PATH.as_posix(),
            "--output",
            PULSE_PATH.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    pulse = json.loads((tmp_path / PULSE_PATH).read_text(encoding="utf-8"))

    assert pulse["contract_name"] == "ea.weekly_product_pulse"
    assert pulse["contract_version"] == 2
    assert pulse["summary"].startswith("PropertyQuarry source/browser candidate proof remains preview_only;")
    assert pulse["active_wave"] == "PropertyQuarry flagship receipt closeout"
    assert pulse["release_health"]["reason"].startswith("The PropertyQuarry flagship receipt")
    assert "Executive Assistant" not in json.dumps(pulse)
    assert pulse["active_wave_status"] == "active"
    assert pulse["release_truth_source"] == FLAGSHIP_RECEIPT_PATH.as_posix()
    assert pulse["journey_gate_source"] == str(JOURNEY_GATES_PATH)
    assert pulse["release_truth_provenance"]["present"] is True
    assert pulse["release_truth_provenance"]["sha256"]
    assert pulse["journey_gate_provenance"]["present"] is True
    assert pulse["journey_gate_provenance"]["sha256"]
    assert pulse["journey_gate_scope"] == "supporting_external_fleet_context"
    assert pulse["journey_gate_authority"] == "non_authoritative_for_propertyquarry_launch"
    assert pulse["journey_gate_snapshot_policy"] == "carry_forward_committed_snapshot_only"
    assert pulse["release_health"]["state"] == "blocked"
    assert pulse["release_health"]["scope"] == "production_launch"
    assert pulse["release_health"]["candidate_state"] == "watch"
    assert pulse["release_health"]["production_launch_state"] == "blocked"
    assert pulse["flagship_readiness"]["state"] == "blocked"
    assert pulse["flagship_readiness"]["candidate_state"] == "watch"
    assert pulse["journey_gate_health"]["state"] == "blocked"
    assert pulse["journey_gate_health"]["blocked_count"] == 3
    assert pulse["journey_gate_health"]["authority"] == "non_authoritative_for_propertyquarry_launch"
    assert pulse["supporting_signals"]["journey_gate_source"] == str(JOURNEY_GATES_PATH)
    assert pulse["supporting_signals"]["flagship_release_receipt_source"] == FLAGSHIP_RECEIPT_PATH.as_posix()
    assert pulse["supporting_signals"]["journey_gate_git_head"] == ""
    assert pulse["supporting_signals"]["flagship_release_receipt_git_head"] == ""
    assert pulse["supporting_signals"]["launch_readiness"].startswith("Hold production launch")
    assert pulse["supporting_signals"]["overall_progress_percent"] is None
    assert pulse["supporting_signals"]["overall_progress_status"] == "production_launch_progress_not_evaluated"
    assert pulse["supporting_signals"]["external_fleet_journey_ready_share_percent"] == 50
    assert pulse["oldest_blocker_days"] is None
    assert pulse["oldest_blocker_days_status"] == "not_evaluated"
    assert pulse["design_drift_count"] is None
    assert pulse["public_promise_drift_count"] is None
    assert pulse["drift_count_status"] == "not_evaluated"
    assert pulse["governor_decisions"]
    assert len(pulse["governor_decisions"]) == 2


@pytest.mark.parametrize(
    ("state", "blocked", "warning", "ready", "total", "ready_share"),
    [
        ("ready", 0, 0, 6, 6, 100),
        ("blocked", 6, 0, 0, 6, 0),
    ],
)
def test_weekly_product_pulse_missing_external_source_carries_only_explicit_committed_snapshot(
    tmp_path: Path,
    state: str,
    blocked: int,
    warning: int,
    ready: int,
    total: int,
    ready_share: int,
) -> None:
    module = _load_materializer_module()
    output = tmp_path / module.DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    provenance = {
        "source_path": "/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json",
        "present": True,
        "sha256": "a" * 64,
        "git_head": "b" * 40,
    }
    existing = {
        "journey_gate_source": "/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json",
        "journey_gate_provenance": provenance,
        "journey_gate_health": {
            "state": state,
            "blocked_count": blocked,
            "warning_count": warning,
            "recommended_action": "Keep the external snapshot supporting-only.",
        },
        "supporting_signals": {
            "external_fleet_journey_ready_share_percent": ready_share,
        },
        "governor_decisions": [
            {
                "cited_signals": [
                    f"supporting_external_fleet_ready_count={ready}",
                    f"supporting_external_fleet_total_count={total}",
                    f"supporting_external_fleet_ready_share={ready_share}",
                ]
            }
        ],
    }
    output.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    carried = module._journey_gate_source(
        tmp_path,
        tmp_path / "missing-external-journey-gates.generated.json",
    )

    assert carried["state"] == state
    assert carried["blocked"] == blocked
    assert carried["warning"] == warning
    assert carried["ready"] == ready
    assert carried["total"] == total
    assert carried["ready_share"] == ready_share
    assert carried["provenance"] == provenance


def test_weekly_product_pulse_missing_external_source_cannot_infer_ready_from_total(
    tmp_path: Path,
) -> None:
    module = _load_materializer_module()
    output = tmp_path / module.DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "journey_gate_provenance": {
                    "source_path": "/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json",
                    "present": True,
                    "sha256": "a" * 64,
                    "git_head": "b" * 40,
                },
                "journey_gate_health": {
                    "state": "ready",
                    "blocked_count": 0,
                    "warning_count": 0,
                },
                "governor_decisions": [
                    {"cited_signals": ["supporting_external_fleet_total_count=6"]}
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    carried = module._journey_gate_source(
        tmp_path,
        tmp_path / "missing-external-journey-gates.generated.json",
    )

    assert carried["state"] == "unavailable"
    assert carried["ready"] == 0
    assert carried["total"] == 6
    assert carried["ready_share"] == 0


def test_weekly_product_pulse_missing_incomplete_external_snapshot_never_reports_ready(
    tmp_path: Path,
) -> None:
    _seed_truth_sources(tmp_path)
    module = _load_materializer_module()
    output = tmp_path / module.DEFAULT_OUTPUT
    output.write_text(
        json.dumps(
            {
                "journey_gate_health": {
                    "state": "ready",
                    "blocked_count": 0,
                    "warning_count": 0,
                },
                "governor_decisions": [
                    {
                        "cited_signals": [
                            "supporting_external_fleet_ready_count=6",
                            "supporting_external_fleet_total_count=6",
                            "supporting_external_fleet_ready_share=100",
                        ]
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    pulse = module.build_pulse(
        tmp_path,
        scorecard_path=SCORECARD_PATH,
        journey_gates_path=Path("missing/JOURNEY_GATES.generated.json"),
        flagship_receipt_path=FLAGSHIP_RECEIPT_PATH,
    )

    assert pulse["release_health"]["state"] == "blocked"
    assert pulse["flagship_readiness"]["state"] == "blocked"
    assert pulse["journey_gate_health"]["state"] == "unavailable"
    assert pulse["supporting_signals"]["external_fleet_journey_ready_share_percent"] == 0
    assert "unavailable or incomplete" in pulse["top_support_or_feedback_clusters"][1]["summary"]
    assert "reports ready" not in pulse["top_support_or_feedback_clusters"][1]["summary"]
    assert any(
        "supporting_external_fleet_tuple_coverage=unavailable" in decision["cited_signals"]
        for decision in pulse["governor_decisions"]
    )


def test_weekly_product_pulse_keeps_allowed_provenance_only_head_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_materializer_module()
    monkeypatch.setattr(
        module,
        "_changed_paths_between_heads",
        lambda old_head, new_head: [
            ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
            "LTDs.md",
            "scripts/materialize_ea_browser_workflow_proof.py",
            "scripts/materialize_weekly_product_pulse.py",
            "tests/test_ea_browser_workflow_proof_materializer.py",
            "tests/test_skills.py",
            "tests/test_weekly_product_pulse_materializer.py",
        ],
    )
    pulse_path = tmp_path / "pulse.json"
    stale = {
        "contract_name": "ea.weekly_product_pulse",
        "summary": "same",
        "release_truth_provenance": {"git_head": "old"},
        "supporting_signals": {"flagship_release_receipt_git_head": "old"},
    }
    fresh = {
        "contract_name": "ea.weekly_product_pulse",
        "summary": "same",
        "release_truth_provenance": {"git_head": "new"},
        "supporting_signals": {"flagship_release_receipt_git_head": "new"},
    }
    pulse_path.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")

    module._write_json_stable(pulse_path, fresh)

    assert json.loads(pulse_path.read_text(encoding="utf-8")) == stale


def test_weekly_product_pulse_rewrites_when_disallowed_provenance_head_change_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_materializer_module()
    monkeypatch.setattr(module, "_changed_paths_between_heads", lambda old_head, new_head: ["ea/app/runtime_unrelated.py"])
    pulse_path = tmp_path / "pulse.json"
    stale = {
        "contract_name": "ea.weekly_product_pulse",
        "summary": "same",
        "release_truth_provenance": {"git_head": "old"},
        "supporting_signals": {"flagship_release_receipt_git_head": "old"},
    }
    fresh = {
        "contract_name": "ea.weekly_product_pulse",
        "summary": "same",
        "release_truth_provenance": {"git_head": "new"},
        "supporting_signals": {"flagship_release_receipt_git_head": "new"},
    }
    pulse_path.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")

    module._write_json_stable(pulse_path, fresh)

    assert json.loads(pulse_path.read_text(encoding="utf-8")) == fresh


def test_weekly_product_pulse_rewrites_changed_release_truth_digest(
    tmp_path: Path,
) -> None:
    module = _load_materializer_module()
    pulse_path = tmp_path / "pulse.json"
    stale = {
        "contract_name": "ea.weekly_product_pulse",
        "summary": "same",
        "release_truth_provenance": {
            "git_head": "a" * 40,
            "sha256": "b" * 64,
        },
    }
    fresh = {
        "contract_name": "ea.weekly_product_pulse",
        "summary": "same",
        "release_truth_provenance": {
            "git_head": "a" * 40,
            "sha256": "c" * 64,
        },
    }
    pulse_path.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")

    module._write_json_stable(pulse_path, fresh)

    assert json.loads(pulse_path.read_text(encoding="utf-8")) == fresh


@pytest.mark.parametrize(
    "relative_path",
    (Path("../outside.json"), Path("/tmp/propertyquarry-pulse-outside.json")),
)
def test_weekly_product_pulse_writer_rejects_output_escape(
    tmp_path: Path,
    relative_path: Path,
) -> None:
    module = _load_materializer_module()
    with pytest.raises(ValueError, match="safe repository-relative path"):
        module._write_json_stable(
            relative_path,
            {"status": "blocked"},
            root=tmp_path,
        )


def test_weekly_product_pulse_writer_rejects_symlinks_and_repairs_ambiguous_json(
    tmp_path: Path,
) -> None:
    module = _load_materializer_module()
    victim = tmp_path / "victim.json"
    victim.write_text('{"preserve":true}\n', encoding="utf-8")
    destination = tmp_path / "pulse.json"
    destination.symlink_to(victim.name)
    with pytest.raises(ValueError, match="symlinked or unreadable"):
        module._write_json_stable(
            Path("pulse.json"),
            {"status": "blocked"},
            root=tmp_path,
        )
    assert victim.read_text(encoding="utf-8") == '{"preserve":true}\n'

    destination.unlink()
    destination.write_text(
        '{"status":"blocked","status":"pass"}\n',
        encoding="utf-8",
    )
    module._write_json_stable(
        Path("pulse.json"),
        {"status": "pass"},
        root=tmp_path,
    )
    assert destination.read_text(encoding="utf-8").count('"status"') == 1

    external_parent = tmp_path / "external-parent"
    external_parent.mkdir()
    (tmp_path / "linked-parent").symlink_to(
        external_parent.name,
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="parent is symlinked"):
        module._write_json_stable(
            Path("linked-parent/pulse.json"),
            {"status": "blocked"},
            root=tmp_path,
        )
    assert not (external_parent / "pulse.json").exists()

    (tmp_path / "directory-output").mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        module._write_json_stable(
            Path("directory-output"),
            {"status": "blocked"},
            root=tmp_path,
        )
    (tmp_path / "file-parent").write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(ValueError, match="parent is symlinked or not a directory"):
        module._write_json_stable(
            Path("file-parent/pulse.json"),
            {"status": "blocked"},
            root=tmp_path,
        )

    module._write_json_stable(
        Path("new/canonical/pulse.json"),
        {"status": "blocked"},
        root=tmp_path,
    )
    created = tmp_path / "new/canonical/pulse.json"
    assert json.loads(created.read_text(encoding="utf-8")) == {
        "status": "blocked"
    }
    assert created.stat().st_mode & 0o777 == 0o644


def test_weekly_product_pulse_writer_repairs_equal_payload_mode_with_cas(
    tmp_path: Path,
) -> None:
    module = _load_materializer_module()
    destination = tmp_path / "pulse.json"
    payload = {"status": "pass"}
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o777)

    module._write_json_stable(
        Path("pulse.json"),
        payload,
        root=tmp_path,
    )

    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert destination.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("mutation", ("in_place", "replacement"))
def test_weekly_product_pulse_writer_preserves_final_window_destination_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = _load_materializer_module()
    destination = tmp_path / "pulse.json"
    destination.write_bytes(b'{"status":"approved"}\n')
    concurrent_bytes = b'{"status":"concurrent-operator-edit"}\n'
    original_exchange = module._rename_exchange
    exchange_calls = 0

    def exchange_with_destination_edit(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            if mutation == "in_place":
                destination.write_bytes(concurrent_bytes)
            else:
                replacement = tmp_path / "operator-replacement.json"
                replacement.write_bytes(concurrent_bytes)
                replacement.replace(destination)
        original_exchange(parent_fd, source, destination_name)

    monkeypatch.setattr(module, "_rename_exchange", exchange_with_destination_edit)

    with pytest.raises(RuntimeError, match="changed before publication"):
        module._write_json_stable(
            Path("pulse.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert destination.read_bytes() == concurrent_bytes


@pytest.mark.parametrize("destination_exists", (True, False))
def test_weekly_product_pulse_writer_detects_staged_path_substitution_at_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_exists: bool,
) -> None:
    module = _load_materializer_module()
    destination = tmp_path / "pulse.json"
    approved_bytes = b'{"status":"approved"}\n'
    substituted_bytes = b'{"status":"substituted-staging-path"}\n'
    if destination_exists:
        destination.write_bytes(approved_bytes)
        helper_name = "_rename_exchange"
    else:
        helper_name = "_rename_noreplace"
    original_rename = getattr(module, helper_name)
    rename_calls = 0

    def rename_with_staged_substitution(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            replacement = tmp_path / "staging-substitute.json"
            replacement.write_bytes(substituted_bytes)
            replacement.replace(tmp_path / source)
        original_rename(parent_fd, source, destination_name)

    monkeypatch.setattr(module, helper_name, rename_with_staged_substitution)

    with pytest.raises(
        module._PreserveStagedOutputError,
        match="staging path changed",
    ):
        module._write_json_stable(
            Path("pulse.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    if destination_exists:
        assert destination.read_bytes() == approved_bytes
        recovery = [
            path
            for path in tmp_path.iterdir()
            if path.name.startswith(".pulse.json.release-write-")
        ]
        assert [path.read_bytes() for path in recovery] == [substituted_bytes]
    else:
        assert not destination.exists()
        recovery = [
            path
            for path in tmp_path.iterdir()
            if path.name.startswith(".pulse.json.release-write-")
        ]
        assert [path.read_bytes() for path in recovery] == [substituted_bytes]


def test_weekly_product_pulse_writer_preserves_displaced_data_if_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_materializer_module()
    destination = tmp_path / "pulse.json"
    destination.write_bytes(b'{"status":"approved"}\n')
    concurrent_bytes = b'{"status":"concurrent-before-failed-rollback"}\n'
    original_exchange = module._rename_exchange
    exchange_calls = 0

    def exchange_with_failed_rollback(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 1:
            destination.write_bytes(concurrent_bytes)
            original_exchange(parent_fd, source, destination_name)
            return
        raise OSError(errno.EIO, "injected exchange rollback failure")

    monkeypatch.setattr(module, "_rename_exchange", exchange_with_failed_rollback)

    with pytest.raises(
        module._PreserveStagedOutputError,
        match="rollback failed",
    ):
        module._write_json_stable(
            Path("pulse.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "fresh"}
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".pulse.json.release-write-")
    ]
    assert [path.read_bytes() for path in recovery] == [concurrent_bytes]


@pytest.mark.parametrize("destination_exists", (True, False))
def test_weekly_product_pulse_writer_rolls_back_staged_symlink_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_exists: bool,
) -> None:
    module = _load_materializer_module()
    destination = tmp_path / "pulse.json"
    approved_bytes = b'{"status":"approved"}\n'
    victim = tmp_path / "symlink-victim.json"
    victim_bytes = b'{"preserve":"symlink-victim"}\n'
    victim.write_bytes(victim_bytes)
    if destination_exists:
        destination.write_bytes(approved_bytes)
        helper_name = "_rename_exchange"
    else:
        helper_name = "_rename_noreplace"
    original_rename = getattr(module, helper_name)
    rename_calls = 0

    def rename_with_staged_symlink(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            staged_path = tmp_path / source
            staged_path.unlink()
            staged_path.symlink_to(victim.name)
        original_rename(parent_fd, source, destination_name)

    monkeypatch.setattr(module, helper_name, rename_with_staged_symlink)

    with pytest.raises(module._PreserveStagedOutputError):
        module._write_json_stable(
            Path("pulse.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    if destination_exists:
        assert not destination.is_symlink()
        assert destination.read_bytes() == approved_bytes
    else:
        assert not destination.exists()
        assert not destination.is_symlink()
    assert victim.read_bytes() == victim_bytes
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".pulse.json.release-write-")
    ]
    assert len(recovery) == 1
    assert recovery[0].is_symlink()
    assert recovery[0].readlink() == Path(victim.name)


def test_weekly_product_pulse_writer_preserves_quarantine_after_identity_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_materializer_module()
    destination = tmp_path / "pulse.json"
    concurrent_bytes = b'{"status":"concurrent-staged-entry"}\n'
    original_noreplace = module._rename_noreplace
    rename_calls = 0

    def noreplace_with_staged_replacement(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        nonlocal rename_calls
        rename_calls += 1
        if rename_calls == 1:
            replacement = tmp_path / "concurrent-staged-entry.json"
            replacement.write_bytes(concurrent_bytes)
            replacement.replace(tmp_path / source)
        original_noreplace(parent_fd, source, destination_name)

    monkeypatch.setattr(module, "_rename_noreplace", noreplace_with_staged_replacement)
    monkeypatch.setattr(
        module,
        "_entry_identity_no_follow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.EIO, "injected post-quarantine identity failure")
        ),
    )

    with pytest.raises(module._PreserveStagedOutputError):
        module._write_json_stable(
            Path("pulse.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert not destination.exists()
    recovery = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".pulse.json.release-write-")
    ]
    assert [path.read_bytes() for path in recovery] == [concurrent_bytes]


def test_weekly_product_pulse_writer_preserves_new_destination_racing_noreplace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_materializer_module()
    destination = tmp_path / "pulse.json"
    concurrent_bytes = b'{"status":"concurrent-new-destination"}\n'
    original_noreplace = module._rename_noreplace

    def noreplace_after_destination_appears(
        parent_fd: int,
        source: str,
        destination_name: str,
    ) -> None:
        destination.write_bytes(concurrent_bytes)
        original_noreplace(parent_fd, source, destination_name)

    monkeypatch.setattr(
        module,
        "_rename_noreplace",
        noreplace_after_destination_appears,
    )

    with pytest.raises(RuntimeError, match="appeared during publication"):
        module._write_json_stable(
            Path("pulse.json"),
            {"status": "fresh"},
            root=tmp_path,
        )

    assert destination.read_bytes() == concurrent_bytes


def test_weekly_product_pulse_blocks_malformed_nested_flagship_receipt(
    tmp_path: Path,
) -> None:
    module = _load_materializer_module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "pass",
                "truth_plane": 1,
                "browser_workflow_proof": {},
                "live_readiness": {"status": "pass"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    observed = module._flagship_receipt_source(tmp_path, Path("receipt.json"))

    assert observed["status"] == "blocked"
    assert (
        "flagship receipt contains invalid nested release evidence"
        in observed["limitations"]
    )


def test_weekly_product_pulse_does_not_claim_missing_browser_proof_after_pass_receipt(tmp_path: Path) -> None:
    _seed_truth_sources(tmp_path)

    receipt = json.loads((tmp_path / FLAGSHIP_RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["status"] = "pass"
    receipt["browser_workflow_proof"]["published_receipt_present"] = True
    (tmp_path / FLAGSHIP_RECEIPT_PATH).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--scorecard",
            SCORECARD_PATH.as_posix(),
            "--journey-gates",
            str(JOURNEY_GATES_PATH),
            "--flagship-receipt",
            FLAGSHIP_RECEIPT_PATH.as_posix(),
            "--output",
            PULSE_PATH.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    pulse = json.loads((tmp_path / PULSE_PATH).read_text(encoding="utf-8"))

    assert pulse["supporting_signals"]["launch_readiness"] == (
        "Source/browser candidate proof is green; hold production launch until protected live readiness passes."
    )
    assert (
        pulse["supporting_signals"]["provider_route_stewardship"]["canary_status"]
        == "Browser execution proof is published for the source/browser candidate; protected live evidence remains separate."
    )
    assert (
        pulse["supporting_signals"]["provider_route_stewardship"]["next_decision"]
        == "Complete the protected launch profile and live receipts, then re-materialize this pulse."
    )


def test_weekly_product_pulse_does_not_promote_fleet_ready_context_to_launch_authority(tmp_path: Path) -> None:
    _seed_truth_sources(tmp_path)

    receipt = json.loads((tmp_path / FLAGSHIP_RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["status"] = "pass"
    receipt["browser_workflow_proof"]["published_receipt_present"] = True
    (tmp_path / FLAGSHIP_RECEIPT_PATH).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    journey = json.loads(Path(JOURNEY_GATES_PATH).read_text(encoding="utf-8"))
    journey["summary"]["overall_state"] = "ready"
    journey["summary"]["ready_count"] = 6
    journey["summary"]["blocked_count"] = 0
    journey["summary"]["recommended_action"] = "Journey proof is steady on current published evidence."
    Path(JOURNEY_GATES_PATH).write_text(json.dumps(journey, indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--scorecard",
            SCORECARD_PATH.as_posix(),
            "--journey-gates",
            str(JOURNEY_GATES_PATH),
            "--flagship-receipt",
            FLAGSHIP_RECEIPT_PATH.as_posix(),
            "--output",
            PULSE_PATH.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    pulse = json.loads((tmp_path / PULSE_PATH).read_text(encoding="utf-8"))

    assert pulse["summary"] == (
        "PropertyQuarry source/browser candidate proof is green, but protected live readiness is "
        "not_evaluated; this pulse does not support a production launch claim."
    )
    assert pulse["release_health"]["state"] == "blocked"
    assert pulse["release_health"]["scope"] == "production_launch"
    assert pulse["release_health"]["candidate_state"] == "clear"
    assert pulse["release_health"]["production_launch_state"] == "blocked"
    assert pulse["journey_gate_health"]["state"] == "ready"
    assert pulse["journey_gate_health"]["scope"] == "supporting_external_fleet_context"
    assert pulse["journey_gate_health"]["authority"] == "non_authoritative_for_propertyquarry_launch"
    assert pulse["supporting_signals"]["launch_readiness"] == (
        "Source/browser candidate proof is green; hold production launch until protected live readiness passes."
    )
    assert pulse["supporting_signals"]["longest_pole"] == "protected live production evidence"
    assert pulse["supporting_signals"]["overall_progress_percent"] is None
    assert pulse["supporting_signals"]["external_fleet_journey_ready_share_percent"] == 100
    assert pulse["next_checkpoint_question"] == (
        "Which protected production receipt or deployment input is the next blocker to clear?"
    )
    assert "cannot prove PropertyQuarry launch readiness" in pulse["top_support_or_feedback_clusters"][1]["summary"]


def test_weekly_product_pulse_cannot_authorize_production_from_nested_live_status(tmp_path: Path) -> None:
    _seed_truth_sources(tmp_path)

    receipt = json.loads((tmp_path / FLAGSHIP_RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["status"] = "pass"
    receipt["browser_workflow_proof"]["published_receipt_present"] = True
    receipt["live_readiness"] = {"status": "pass"}
    (tmp_path / FLAGSHIP_RECEIPT_PATH).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--scorecard",
            SCORECARD_PATH.as_posix(),
            "--journey-gates",
            str(JOURNEY_GATES_PATH),
            "--flagship-receipt",
            FLAGSHIP_RECEIPT_PATH.as_posix(),
            "--output",
            PULSE_PATH.as_posix(),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    pulse = json.loads((tmp_path / PULSE_PATH).read_text(encoding="utf-8"))

    assert pulse["release_health"]["reported_live_readiness_state"] == "pass_unverified"
    assert pulse["flagship_readiness"]["reported_live_readiness_state"] == "pass_unverified"
    assert pulse["release_health"]["production_launch_state"] == "blocked"
    assert pulse["flagship_readiness"]["production_launch_state"] == "blocked"
    assert pulse["supporting_signals"]["launch_readiness"].endswith(
        "reconcile the current deployment before widening production claims."
    )


@pytest.mark.parametrize("unsafe_alias", ["ready", "clear"])
def test_weekly_product_pulse_does_not_accept_live_readiness_aliases(
    tmp_path: Path, unsafe_alias: str
) -> None:
    _seed_truth_sources(tmp_path)

    receipt = json.loads((tmp_path / FLAGSHIP_RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["status"] = "pass"
    receipt["browser_workflow_proof"]["published_receipt_present"] = True
    receipt["live_readiness"]["status"] = unsafe_alias
    (tmp_path / FLAGSHIP_RECEIPT_PATH).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    module = _load_materializer_module()
    pulse = module.build_pulse(
        tmp_path,
        scorecard_path=SCORECARD_PATH,
        journey_gates_path=JOURNEY_GATES_PATH,
        flagship_receipt_path=FLAGSHIP_RECEIPT_PATH,
    )

    assert pulse["release_health"]["reported_live_readiness_state"] == "not_passed"
    assert pulse["release_health"]["production_launch_state"] == "blocked"
