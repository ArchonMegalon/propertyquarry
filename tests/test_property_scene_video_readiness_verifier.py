from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    path = ROOT / "scripts" / "verify_property_scene_video_readiness.py"
    spec = importlib.util.spec_from_file_location("verify_property_scene_video_readiness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _receipt() -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.scene_video_readiness.v1",
        "telegram_delivery_readiness": {"status": "ready", "blockers": []},
        "providers": [
            {
                "requested_provider": "mootion",
                "provider_key": "mootion",
                "provider_backend_key": "mootion",
                "ready": True,
                "status": "ready",
                "blockers": [],
                "execution_lane": "browseract_remote",
                "checks": {"mootion_browseract_remote": {"ready": True, "target_count": 1}},
            },
            {
                "requested_provider": "magicfit",
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "ready": False,
                "status": "blocked",
                "blockers": ["magicfit_insufficient_credits"],
                "account_inventory": {"expected_account_count": 3, "runtime_account_count": 1, "visible_account_gap": 2},
            },
            {
                "requested_provider": "magic",
                "provider_key": "omagic",
                "provider_backend_key": "omagic",
                "ready": False,
                "status": "blocked",
                "blockers": ["omagic_model_upload_adapter_missing", "omagic_credentials_missing"],
                "account_inventory": {"expected_account_count": 8, "runtime_account_count": 0, "visible_account_gap": 8},
            },
            {
                "requested_provider": "omagic",
                "provider_key": "omagic",
                "provider_backend_key": "omagic",
                "ready": False,
                "status": "blocked",
                "blockers": ["omagic_model_upload_adapter_missing", "omagic_credentials_missing"],
                "account_inventory": {"expected_account_count": 8, "runtime_account_count": 0, "visible_account_gap": 8},
            },
            {
                "requested_provider": "onemin_i2v",
                "provider_key": "onemin_i2v",
                "provider_backend_key": "onemin_i2v",
                "ready": True,
                "status": "ready",
                "blockers": [],
            },
        ],
        "next_actions": [
            {"provider": "magicfit", "reason": "provider_account_visibility_gap", "do_not_touch": ["ONEMIN_*"]},
            {"provider": "magicfit", "reason": "magicfit_insufficient_credits", "do_not_touch": ["ONEMIN_*"]},
            {"provider": "magic", "reason": "provider_account_visibility_gap", "do_not_touch": ["ONEMIN_*"]},
            {"provider": "omagic", "reason": "provider_account_visibility_gap", "do_not_touch": ["ONEMIN_*"]},
            {"provider": "omagic", "reason": "omagic_credentials_missing", "do_not_touch": ["ONEMIN_*"]},
            {"provider": "omagic", "reason": "omagic_model_upload_adapter_missing"},
        ],
    }


def test_scene_video_readiness_verifier_passes_known_healthy_gaps() -> None:
    module = _load_script()

    result = module.validate_receipt(_receipt())

    assert result["status"] == "pass"
    assert result["blockers"] == []


def test_scene_video_readiness_verifier_can_scope_advanced_visual_providers() -> None:
    module = _load_script()
    receipt = _receipt()
    receipt["providers"] = [  # type: ignore[index]
        row
        for row in receipt["providers"]  # type: ignore[index]
        if row["requested_provider"] in {"magicfit", "magic", "omagic"}
    ]

    result = module.validate_receipt(
        receipt,
        required_providers=("magicfit", "magic", "omagic"),
    )

    assert result["status"] == "pass"
    assert result["checked_providers"] == ["magicfit", "magic", "omagic"]


def test_scene_video_readiness_verifier_rejects_magic_routed_to_onemin() -> None:
    module = _load_script()
    receipt = _receipt()
    receipt["providers"][2]["provider_backend_key"] = "onemin_i2v"  # type: ignore[index]

    result = module.validate_receipt(receipt)

    assert result["status"] == "fail"
    assert "magic_backend_mismatch" in result["blockers"]


def test_scene_video_readiness_verifier_cli_fails_on_missing_action(tmp_path: Path) -> None:
    receipt = _receipt()
    receipt["next_actions"] = []
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    script = ROOT / "scripts" / "verify_property_scene_video_readiness.py"

    result = subprocess.run(
        [sys.executable, str(script), "--receipt", str(receipt_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    body = json.loads(result.stdout)
    assert body["status"] == "fail"
    assert body["generated_at"].endswith("Z")
    assert "next_action_missing:magicfit:provider_account_visibility_gap" in body["blockers"]


def test_scene_video_readiness_verifier_requires_action_for_missing_omagic_adapter_target() -> None:
    module = _load_script()
    receipt = _receipt()
    receipt["providers"][2]["blockers"] = ["omagic_model_upload_endpoint_missing"]  # type: ignore[index]
    receipt["providers"][3]["blockers"] = ["omagic_model_upload_endpoint_missing"]  # type: ignore[index]
    receipt["next_actions"] = [
        action
        for action in receipt["next_actions"]  # type: ignore[index]
        if not (isinstance(action, dict) and action.get("reason") == "omagic_model_upload_adapter_missing")
    ]

    result = module.validate_receipt(receipt)

    assert result["status"] == "fail"
    assert "next_action_missing:omagic:omagic_model_upload_endpoint_missing" in result["blockers"]


def test_scene_video_readiness_verifier_requires_action_for_disabled_omagic_adapter() -> None:
    module = _load_script()
    receipt = _receipt()
    receipt["providers"][2]["blockers"] = ["omagic_model_upload_adapter_disabled"]  # type: ignore[index]
    receipt["providers"][3]["blockers"] = ["omagic_model_upload_adapter_disabled"]  # type: ignore[index]
    receipt["next_actions"] = [
        action
        for action in receipt["next_actions"]  # type: ignore[index]
        if not (isinstance(action, dict) and action.get("reason") == "omagic_model_upload_adapter_missing")
    ]

    result = module.validate_receipt(receipt)

    assert result["status"] == "fail"
    assert "next_action_missing:omagic:omagic_model_upload_adapter_disabled" in result["blockers"]


def test_scene_video_readiness_verifier_requires_actions_for_missing_mootion_remote_lane() -> None:
    module = _load_script()
    receipt = _receipt()
    mootion = receipt["providers"][0]  # type: ignore[index]
    mootion.pop("execution_lane", None)  # type: ignore[union-attr]
    mootion["checks"] = {"mootion_browseract_remote": {"ready": False, "target_count": 0}}  # type: ignore[index]

    result = module.validate_receipt(receipt)

    assert result["status"] == "fail"
    assert "mootion_browseract_remote_lane_missing" in result["blockers"]
    assert "mootion_browseract_bridge_not_ready" in result["blockers"]
    assert "next_action_missing:mootion:mootion_browseract_remote_lane_missing" in result["blockers"]
    assert "next_action_missing:mootion:mootion_browseract_bridge_not_ready" in result["blockers"]


def test_scene_video_readiness_verifier_requires_action_for_magicfit_credit_constraint() -> None:
    module = _load_script()
    receipt = _receipt()
    magicfit = receipt["providers"][1]  # type: ignore[index]
    magicfit["ready"] = True  # type: ignore[index]
    magicfit["status"] = "ready"  # type: ignore[index]
    magicfit["blockers"] = []  # type: ignore[index]
    magicfit["runtime_account_count"] = 2  # type: ignore[index]
    magicfit["credit_state"] = "constrained"  # type: ignore[index]
    magicfit["account_inventory"] = {  # type: ignore[index]
        "expected_account_count": 2,
        "runtime_account_count": 2,
        "tracked_account_count": 3,
        "unavailable_account_count": 1,
        "availability_reason": "one_account_depleted",
        "visible_account_gap": 0,
    }
    receipt["next_actions"] = [
        action
        for action in receipt["next_actions"]  # type: ignore[index]
        if not (
            isinstance(action, dict)
            and action.get("provider") == "magicfit"
            and action.get("reason") in {"provider_account_visibility_gap", "magicfit_insufficient_credits"}
        )
    ]

    result = module.validate_receipt(receipt)

    assert result["status"] == "fail"
    assert "next_action_missing:magicfit:magicfit_credit_constrained" in result["blockers"]


def test_scene_video_readiness_verifier_accepts_actions_for_missing_mootion_remote_lane() -> None:
    module = _load_script()
    receipt = _receipt()
    mootion = receipt["providers"][0]  # type: ignore[index]
    mootion.pop("execution_lane", None)  # type: ignore[union-attr]
    mootion["checks"] = {"mootion_browseract_remote": {"ready": False, "target_count": 0}}  # type: ignore[index]
    receipt["next_actions"].extend(  # type: ignore[union-attr]
        [
            {"provider": "mootion", "reason": "mootion_browseract_remote_lane_missing"},
            {"provider": "mootion", "reason": "mootion_browseract_bridge_not_ready"},
        ]
    )

    result = module.validate_receipt(receipt)

    assert result["status"] == "fail"
    assert "mootion_browseract_remote_lane_missing" in result["blockers"]
    assert "mootion_browseract_bridge_not_ready" in result["blockers"]
    assert "next_action_missing:mootion:mootion_browseract_remote_lane_missing" not in result["blockers"]
    assert "next_action_missing:mootion:mootion_browseract_bridge_not_ready" not in result["blockers"]


def test_property_release_gate_runs_scene_video_readiness_report_and_verifier() -> None:
    release_gate = (ROOT / "scripts" / "property_release_gates.sh").read_text(encoding="utf-8")

    assert "property_scene_video_readiness_report.py" in release_gate
    assert "verify_property_scene_video_readiness.py" in release_gate
    assert "_completion/scene_video_readiness/release-gate.json" in release_gate
    assert "_completion/scene_video_readiness/release-gate-verifier.json" in release_gate
