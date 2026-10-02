from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_operator_status as operator_status
from scripts import propertyquarry_ooda_runtime_review as review


NOW = datetime(2026, 8, 26, 7, 15, tzinfo=timezone.utc)


def _operator_status() -> dict[str, object]:
    source_generated_at = (NOW - timedelta(minutes=10)).isoformat()
    action = {
        "lane": "gold_live_runtime",
        "reason": "live_runtime_host_admission_rejected",
        "source_generated_at": source_generated_at,
        "safe_next_action": operator_status._GOLD_ACTIONS[
            "live_runtime_host_admission_rejected"
        ],
        "consent_required": True,
        "automatic_execution_allowed": False,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
        "provider_quota_consumption_allowed": False,
    }
    return {
        "schema": operator_status.SCHEMA,
        "status": "action_required",
        "action_required": True,
        "interrupt_operator": True,
        "updated_at": NOW.isoformat(),
        "blocking_reason": "live_runtime_host_admission_rejected",
        "next_action": action["safe_next_action"],
        "progress": {
            "approved_snapshot_verified": True,
            "current_snapshot_hashes_verified": True,
            "action_required_count": 1,
            "novel_action_count": 1,
        },
        "automatic_execution_allowed": False,
        "provider_quota_consumption_allowed": False,
        "protected_operation_executed": False,
        "actions": [action],
    }


def _tunnel_operator_status() -> dict[str, object]:
    status = _operator_status()
    actions = status["actions"]
    assert isinstance(actions, list) and isinstance(actions[0], dict)
    action = actions[0]
    action["reason"] = "live_runtime_tunnel_unavailable"
    action["safe_next_action"] = operator_status._GOLD_ACTIONS[
        "live_runtime_tunnel_unavailable"
    ]
    status["blocking_reason"] = "live_runtime_tunnel_unavailable"
    status["next_action"] = action["safe_next_action"]
    return status


def _source_evidence() -> dict[str, dict[str, object]]:
    generated_at = (NOW - timedelta(minutes=5)).isoformat()
    return {
        "cycle_receipt": {
            "sha256": "a" * 64,
            "bytes": 1024,
            "generated_at": generated_at,
        },
        "approval_manifest": {
            "sha256": "b" * 64,
            "bytes": 2048,
            "generated_at": generated_at,
        },
    }


def _runtime(*, services: list[dict[str, str]] | None = None) -> dict[str, object]:
    rows = [
        {
            **row,
            "health": row.get("health")
            or ("healthy" if row.get("state") == "running" else "none"),
        }
        for row in list(services or [])
    ]
    rows.sort(key=lambda row: (row["service"], row["state"], row["health"]))
    fingerprint = review._sha256(
        review._canonical({"project": "property", "services": rows})
    )
    return {
        "source": "local_docker",
        "observed_at": NOW.isoformat(),
        "query_status": "pass",
        "project": "property",
        "container_count": len(rows),
        "services": rows,
        "fingerprint_sha256": fingerprint,
    }


def _release() -> dict[str, object]:
    return {
        "source": "local_git",
        "observed_at": NOW.isoformat(),
        "head_sha": "c" * 40,
        "worktree_clean": False,
        "changed_path_count": 17,
        "worktree_fingerprint_sha256": "d" * 64,
        "changed_paths_recorded": False,
    }


def test_runtime_observation_receipt_proves_absence_without_granting_authority() -> None:
    receipt = review.build_runtime_observation_receipt(_runtime(), now=NOW)

    verification = review.verify_runtime_observation_receipt(receipt, now=NOW)

    assert receipt["runtime_condition"] == "absent"
    assert receipt["scheduler_service"] == "propertyquarry-scheduler"
    assert receipt["scheduler_condition"] == "absent"
    assert receipt["scheduler_state"] == ""
    assert receipt["scheduler_health"] == "none"
    assert receipt["scheduler_container_healthy"] is False
    assert receipt["persistent_reevaluation_running"] is False
    assert receipt["action_required"] is False
    assert receipt["interrupt_operator"] is False
    assert receipt["consent_gate"] == {
        "required_for": ["deployment_or_restart"],
        "authorization_recorded": False,
        "execution_authorized": False,
    }
    assert verification["status"] == "verified"
    assert verification["runtime_condition"] == "absent"
    assert verification["progress"] == {
        "container_count": 0,
        "running_container_count": 0,
        "non_running_container_count": 0,
        "scheduler_container_count": 0,
        "running_scheduler_container_count": 0,
        "healthy_scheduler_container_count": 0,
        "current_evidence_verified": False,
    }
    assert verification["deployment_or_restart_authorized"] is False
    assert verification["provider_quota_consumption_allowed"] is False
    assert verification["delivery_authorized"] is False


def test_runtime_review_subprocess_timeouts_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def time_out(command, **_kwargs):
        raise review.subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr(review.subprocess, "run", time_out)

    text_result = review._run(["/usr/bin/docker", "ps"], timeout=1)
    bytes_result = review._run_bytes(
        ["/usr/bin/git", "diff"],
        timeout=1,
    )

    assert text_result.returncode == 124
    assert text_result.stdout == ""
    assert text_result.stderr == ""
    assert bytes_result.returncode == 124
    assert bytes_result.stdout == b""
    assert bytes_result.stderr == b""


def _deployment_environment_root(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.property.yml").write_text(
        "services:\n  api:\n    environment:\n"
        "      FIRST: ${PROPERTYQUARRY_FIRST_REQUIRED:?required}\n",
        encoding="utf-8",
    )
    (tmp_path / "docker-compose.cloudflared.yml").write_text(
        "services:\n  tunnel:\n    environment:\n"
        "      SECOND: ${PROPERTYQUARRY_SECOND_REQUIRED:?required}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_deployment_environment_inspection_records_presence_without_values(
    tmp_path: Path,
) -> None:
    root = _deployment_environment_root(tmp_path)
    env_path = root / ".env"
    env_path.write_text(
        "PROPERTYQUARRY_FIRST_REQUIRED=secret-one\n"
        "PROPERTYQUARRY_SECOND_REQUIRED='secret-two'\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    posture = review.inspect_deployment_environment(root=root)

    assert posture["status"] == "ready"
    assert posture["file_status"] == "admissible"
    assert posture["file_mode"] == 0o600
    assert posture["inspection_complete"] is True
    assert posture["required_keys"] == [
        "PROPERTYQUARRY_FIRST_REQUIRED",
        "PROPERTYQUARRY_SECOND_REQUIRED",
    ]
    assert posture["required_key_count"] == 2
    assert posture["present_nonempty_required_key_count"] == 2
    assert posture["missing_required_keys"] == []
    assert posture["configuration_complete"] is True
    assert posture["environment_values_recorded"] is False
    assert posture["environment_values_hashed"] is False
    assert posture["secret_values_recorded"] is False
    serialized = json.dumps(posture)
    assert "secret-one" not in serialized
    assert "secret-two" not in serialized


def test_recovery_preview_uses_authoritative_ordered_environment_layers() -> None:
    command = review._compose_command("start")
    environment_paths = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--env-file"
    ]
    compose_paths = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--file"
    ]

    assert review.DEFAULT_DEPLOYMENT_ENV_LAYER_PATHS == tuple(
        path.relative_to(review.runtime_deploy.PROPERTY_ROOT)
        for path in review.runtime_deploy.ENV_FILES
    )
    assert environment_paths == [
        path.as_posix() for path in review.DEFAULT_DEPLOYMENT_ENV_LAYER_PATHS
    ]
    assert compose_paths == [
        path.as_posix() for path in review.local_deployment.COMPOSE_FILES
    ]
    assert max(command.index(path) for path in environment_paths) < min(
        command.index(path) for path in compose_paths
    )


def test_deployment_environment_inspection_names_only_missing_inputs(
    tmp_path: Path,
) -> None:
    root = _deployment_environment_root(tmp_path)
    env_path = root / ".env"
    env_path.write_text(
        "PROPERTYQUARRY_FIRST_REQUIRED=super-secret-alpha-731\n"
        "PROPERTYQUARRY_SECOND_REQUIRED=\n"
        "UNRELATED_VALUE=super-secret-beta-842\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    posture = review.inspect_deployment_environment(root=root)

    assert posture["status"] == "incomplete"
    assert posture["blocking_reason"] == (
        "required_deployment_environment_keys_missing"
    )
    assert posture["present_nonempty_required_key_count"] == 1
    assert posture["missing_required_keys"] == [
        "PROPERTYQUARRY_SECOND_REQUIRED"
    ]
    assert posture["missing_required_key_count"] == 1
    assert posture["unresolved_required_keys"] == []
    assert posture["configuration_complete"] is False
    assert "super-secret-alpha-731" not in json.dumps(posture)
    assert "super-secret-beta-842" not in json.dumps(posture)


@pytest.mark.parametrize("failure", ["missing", "unsafe", "duplicate"])
def test_deployment_environment_inspection_fails_closed_without_values(
    tmp_path: Path,
    failure: str,
) -> None:
    root = _deployment_environment_root(tmp_path)
    env_path = root / ".env"
    if failure != "missing":
        env_path.write_text(
                (
                    "PROPERTYQUARRY_FIRST_REQUIRED=duplicate-secret-alpha-953\n"
                    "PROPERTYQUARRY_FIRST_REQUIRED=duplicate-secret-beta-164\n"
                    if failure == "duplicate"
                    else "PROPERTYQUARRY_FIRST_REQUIRED=unsafe-secret-gamma-275\n"
            ),
            encoding="utf-8",
        )
        env_path.chmod(0o600 if failure == "duplicate" else 0o640)

    posture = review.inspect_deployment_environment(root=root)

    assert posture["status"] == "blocked"
    assert posture["inspection_complete"] is False
    assert posture["configuration_complete"] is False
    assert posture["missing_required_keys"] == []
    assert posture["unresolved_required_keys"] == posture["required_keys"]
    assert posture["present_nonempty_required_key_count"] == 0
    assert posture["environment_values_recorded"] is False
    assert posture["environment_values_hashed"] is False
    assert "unsafe-secret-gamma-275" not in json.dumps(posture)
    assert "duplicate-secret-alpha-953" not in json.dumps(posture)
    assert "duplicate-secret-beta-164" not in json.dumps(posture)


def test_deployment_environment_posture_changes_only_with_presence_state(
    tmp_path: Path,
) -> None:
    root = _deployment_environment_root(tmp_path)
    env_path = root / ".env"
    env_path.write_text(
        "PROPERTYQUARRY_FIRST_REQUIRED=one\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    before = review.inspect_deployment_environment(root=root)

    env_path.write_text(
        "PROPERTYQUARRY_FIRST_REQUIRED=changed-secret\n"
        "PROPERTYQUARRY_SECOND_REQUIRED=two\n",
        encoding="utf-8",
    )
    after = review.inspect_deployment_environment(root=root)

    assert before["configuration_complete"] is False
    assert after["configuration_complete"] is True
    assert before != after
    assert "changed-secret" not in json.dumps(after)


def test_deployment_environment_intake_names_only_genuine_external_inputs() -> None:
    required = sorted(
        set().union(
            *(
                set(contract["keys"])
                for contract in review._DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES
            )
        )
    )
    present = {
        "EA_SIGNING_SECRET",
        "ONEMIN_DIRECT_API_KEYS_JSON_FILE",
        "POSTGRES_PASSWORD",
        "PROPERTYQUARRY_CF_TUNNEL_TOKEN",
    }
    missing = sorted(set(required) - present)

    plan = review._deployment_environment_intake_plan(
        required_keys=required,
        missing_required_keys=missing,
        unresolved_required_keys=[],
        requirement_fingerprint_sha256="a" * 64,
    )

    assert plan["status"] == "operator_input_required"
    assert plan["blocking_reason"] == "external_account_material_required"
    assert plan["operator_external_input_keys"] == [
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
    ]
    assert plan["operator_external_input_key_count"] == 2
    assert plan["locally_stageable_key_count"] == 17
    assert plan["locally_staged_key_count"] == 0
    assert plan["locally_pending_key_count"] == 17
    assert plan["unclassified_required_keys"] == []
    assert plan["configuration_apply_authorized"] is False
    assert plan["deployment_or_restart_authorized"] is False
    assert plan["environment_values_recorded"] is False
    assert plan["environment_values_hashed"] is False
    assert plan["secret_values_recorded"] is False


def test_deployment_environment_intake_fails_closed_for_unknown_requirement() -> None:
    plan = review._deployment_environment_intake_plan(
        required_keys=["PROPERTYQUARRY_FUTURE_REQUIRED"],
        missing_required_keys=["PROPERTYQUARRY_FUTURE_REQUIRED"],
        unresolved_required_keys=[],
        requirement_fingerprint_sha256="b" * 64,
    )

    assert plan["status"] == "classification_required"
    assert plan["operator_external_input_required"] is False
    assert plan["operator_external_input_keys"] == []
    assert plan["locally_stageable_keys"] == []
    assert plan["unclassified_required_keys"] == [
        "PROPERTYQUARRY_FUTURE_REQUIRED"
    ]
    assert plan["configuration_apply_authorized"] is False


def test_operator_import_is_exact_private_and_value_free(tmp_path: Path) -> None:
    import_path = tmp_path / "operator-import.env"
    import_path.write_text(
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=oauth-client-secret-value\n"
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET=oauth-provider-secret-value\n",
        encoding="utf-8",
    )
    import_path.chmod(0o600)

    posture = review.inspect_deployment_environment_operator_import(
        required_keys=[
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
        ],
        root=tmp_path,
        import_path=Path("operator-import.env"),
    )

    assert posture["status"] == "ready_for_review"
    assert posture["file_status"] == "admissible"
    assert posture["file_mode"] == 0o600
    assert posture["present_nonempty_required_key_count"] == 2
    assert posture["missing_required_keys"] == []
    assert posture["unexpected_keys"] == []
    assert posture["import_ready_for_review"] is True
    assert posture["configuration_merge_authorized"] is False
    assert posture["configuration_merged"] is False
    assert posture["environment_values_recorded"] is False
    assert posture["environment_values_hashed"] is False
    serialized = json.dumps(posture)
    assert "oauth-client-secret-value" not in serialized
    assert "oauth-provider-secret-value" not in serialized


@pytest.mark.parametrize("failure", ["missing", "unsafe", "duplicate", "extra"])
def test_operator_import_fails_closed_without_values(
    tmp_path: Path,
    failure: str,
) -> None:
    import_path = tmp_path / "operator-import.env"
    if failure != "missing":
        if failure == "duplicate":
            content = (
                "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=first-secret\n"
                "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=second-secret\n"
            )
        elif failure == "extra":
            content = (
                "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=first-secret\n"
                "UNEXPECTED_SECRET_KEY=unexpected-secret\n"
            )
        else:
            content = "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=unsafe-secret\n"
        import_path.write_text(content, encoding="utf-8")
        import_path.chmod(0o640 if failure == "unsafe" else 0o600)

    posture = review.inspect_deployment_environment_operator_import(
        required_keys=["PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"],
        root=tmp_path,
        import_path=Path("operator-import.env"),
    )

    assert posture["import_ready_for_review"] is False
    assert posture["configuration_merge_authorized"] is False
    assert posture["configuration_merged"] is False
    assert posture["environment_values_recorded"] is False
    assert posture["environment_values_hashed"] is False
    if failure == "missing":
        assert posture["status"] == "awaiting_operator_import"
        assert posture["missing_required_keys"] == [
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"
        ]
    elif failure == "extra":
        assert posture["status"] == "blocked"
        assert posture["unexpected_keys"] == ["UNEXPECTED_SECRET_KEY"]
    else:
        assert posture["status"] == "blocked"
    serialized = json.dumps(posture)
    assert "first-secret" not in serialized
    assert "second-secret" not in serialized
    assert "unexpected-secret" not in serialized
    assert "unsafe-secret" not in serialized


def test_operator_import_value_changes_do_not_create_a_value_fingerprint(
    tmp_path: Path,
) -> None:
    import_path = tmp_path / "operator-import.env"
    import_path.write_text(
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=first-secret\n",
        encoding="utf-8",
    )
    import_path.chmod(0o600)
    before = review.inspect_deployment_environment_operator_import(
        required_keys=["PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"],
        root=tmp_path,
        import_path=Path("operator-import.env"),
    )
    import_path.write_text(
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=changed-secret\n",
        encoding="utf-8",
    )
    after = review.inspect_deployment_environment_operator_import(
        required_keys=["PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"],
        root=tmp_path,
        import_path=Path("operator-import.env"),
    )

    assert before == after
    assert "first-secret" not in json.dumps(before)
    assert "changed-secret" not in json.dumps(after)


def test_intake_plan_stages_ready_import_for_separate_merge_review(
    tmp_path: Path,
) -> None:
    import_path = tmp_path / "operator-import.env"
    import_path.write_text(
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID=external-value\n",
        encoding="utf-8",
    )
    import_path.chmod(0o600)
    operator_import = review.inspect_deployment_environment_operator_import(
        required_keys=["PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"],
        root=tmp_path,
        import_path=Path("operator-import.env"),
    )

    plan = review._deployment_environment_intake_plan(
        required_keys=["PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"],
        missing_required_keys=["PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"],
        unresolved_required_keys=[],
        requirement_fingerprint_sha256="c" * 64,
        operator_import_posture=operator_import,
    )

    assert plan["status"] == "operator_merge_review_required"
    assert plan["operator_external_input_required"] is False
    assert plan["operator_external_input_keys"] == []
    assert plan["operator_merge_review_required"] is True
    assert plan["operator_merge_review_keys"] == [
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID"
    ]
    assert plan["configuration_apply_authorized"] is False


def _local_runtime_binding_root(tmp_path: Path) -> Path:
    for relative in review._LOCAL_RUNTIME_BINDING_PATHS.values():
        target = tmp_path / relative
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
    return tmp_path


def test_local_runtime_binding_candidate_is_staged_without_secret_projection(
    tmp_path: Path,
) -> None:
    root = _local_runtime_binding_root(tmp_path)
    candidate = Path(
        "_completion/propertyquarry_ooda_notification_cycle/"
        "local-runtime.env"
    )

    posture = review.materialize_local_runtime_binding_candidate(
        root=root,
        candidate_path=candidate,
    )

    assert posture["status"] == "ready_for_merge_review"
    assert posture["file_status"] == "admissible"
    assert posture["file_mode"] == 0o600
    assert posture["required_keys"] == sorted(review._LOCAL_CANDIDATE_KEYS)
    assert posture["required_key_count"] == 8
    assert posture["derived_runtime_binding_key_count"] == 7
    assert posture["locally_generated_secret_key_count"] == 1
    assert posture["source_directory_count"] == 5
    assert posture["source_directories_ready"] is True
    assert posture["common_owner_identity_resolved"] is True
    assert posture["candidate_matches_current_derivation"] is True
    assert posture["candidate_secret_policy_verified"] is True
    assert posture["candidate_ready_for_merge_review"] is True
    assert posture["candidate_values_recorded_in_receipt"] is False
    assert posture["candidate_values_hashed"] is False
    assert posture["candidate_contains_secret_values"] is True
    assert posture["configuration_merge_authorized"] is False
    assert posture["configuration_merged"] is False
    assert posture["deployment_or_restart_authorized"] is False
    serialized = json.dumps(posture)
    assert str(root) not in serialized
    candidate_text = (root / candidate).read_text(encoding="utf-8")
    assert "PROPERTYQUARRY_OODA_STAGE_UID=" in candidate_text
    assert "PROPERTYQUARRY_OODA_STAGE_GID=" in candidate_text
    candidate_values = review._dotenv_values(candidate_text)
    generated_secret = candidate_values[
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"
    ]
    assert review._LOCAL_GENERATED_SECRET.fullmatch(generated_secret)
    assert generated_secret not in serialized
    before = (root / candidate).read_bytes()
    assert review.materialize_local_runtime_binding_candidate(
        root=root,
        candidate_path=candidate,
    )["status"] == "ready_for_merge_review"
    assert (root / candidate).read_bytes() == before

    required = sorted(
        set().union(
            *(
                set(contract["keys"])
                for contract in review._DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES
            )
        )
    )
    present = {
        "EA_SIGNING_SECRET",
        "ONEMIN_DIRECT_API_KEYS_JSON_FILE",
        "POSTGRES_PASSWORD",
        "PROPERTYQUARRY_CF_TUNNEL_TOKEN",
    }
    plan = review._deployment_environment_intake_plan(
        required_keys=required,
        missing_required_keys=sorted(set(required) - present),
        unresolved_required_keys=[],
        requirement_fingerprint_sha256="d" * 64,
        local_runtime_candidate_posture=posture,
    )
    assert plan["locally_stageable_key_count"] == 17
    assert plan["locally_staged_key_count"] == 8
    assert plan["locally_pending_key_count"] == 9
    assert plan["local_merge_review_required"] is False


def test_layered_runtime_environment_reuses_existing_private_credentials(
    tmp_path: Path,
) -> None:
    root = _local_runtime_binding_root(tmp_path)
    required = sorted(
        set().union(
            *(
                set(contract["keys"])
                for contract in review._DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES
            )
        )
    )
    (root / "docker-compose.property.yml").write_text(
        "services:\n  api:\n    environment:\n"
        + "".join(
            f"      {key}: ${{{key}:?required}}\n" for key in required
        ),
        encoding="utf-8",
    )
    (root / "docker-compose.cloudflared.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    layer_values = {
        Path(".env"): {
            "EA_SIGNING_SECRET": "base-signing-secret",
            "ONEMIN_DIRECT_API_KEYS_JSON_FILE": "/private/onemin.json",
            "POSTGRES_PASSWORD": "base-postgres-secret",
            "PROPERTYQUARRY_CF_TUNNEL_TOKEN": "base-tunnel-secret",
        },
        Path("state/runtime/property_scene_video_shared.env"): {
            "UNRELATED_SCENE_VALUE": "scene-value",
        },
        Path("state/runtime/propertyquarry_database_roles.env"): {
            key: f"postgresql://{index}:database-role-secret@db/propertyquarry"
            for index, key in enumerate(
                sorted(
                    next(
                        contract["keys"]
                        for contract in review._DEPLOYMENT_ENVIRONMENT_REQUIREMENT_CLASSES
                        if contract["classification"] == "database_role_binding"
                    )
                ),
                1,
            )
        },
        Path("state/runtime/propertyquarry_admission.env"): {
            "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": (
                "postgresql://admission:admission-secret@db/propertyquarry"
            ),
            "PROPERTYQUARRY_API_INGRESS_DATABASE_URL": (
                "postgresql://ingress:ingress-secret@db/propertyquarry"
            ),
        },
        Path("state/runtime/propertyquarry_google_identity.env"): {
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID": "layer-oauth-client",
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET": "layer-oauth-secret",
            "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET": "layer-state-secret",
            "PROPERTYQUARRY_IDENTITY_SESSION_SECRET": "layer-session-secret",
        },
        Path("state/runtime/propertyquarry_registration_email.env"): {
            "UNRELATED_REGISTRATION_VALUE": "registration-value",
        },
    }
    for relative, values in layer_values.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
            encoding="utf-8",
        )
        target.chmod(0o600)
    candidate = review.materialize_local_runtime_binding_candidate(root=root)
    assert candidate["status"] == "ready_for_merge_review"

    posture = review.inspect_deployment_environment(
        root=root,
        environment_paths=review.DEFAULT_DEPLOYMENT_ENV_LAYER_PATHS,
    )

    assert posture["source"] == "private_dotenv_layer_presence_inspection"
    assert posture["environment_sources"] == [
        path.as_posix() for path in review.DEFAULT_DEPLOYMENT_ENV_LAYER_PATHS
    ]
    assert posture["environment_source_count"] == 6
    assert len(posture["environment_layers"]) == 6
    assert all(
        layer["file_status"] == "admissible"
        and layer["file_mode"] == 0o600
        and layer["environment_values_recorded"] is False
        and layer["environment_values_hashed"] is False
        for layer in posture["environment_layers"]
    )
    assert posture["missing_required_keys"] == sorted(
        review._LOCAL_CANDIDATE_KEYS
    )
    assert posture["present_nonempty_required_key_count"] == 15
    assert posture["configuration_complete"] is False
    intake = posture["intake_plan"]
    assert intake["status"] == "local_merge_review_required"
    assert intake["operator_external_input_required"] is False
    assert intake["operator_external_input_keys"] == []
    assert intake["operator_external_input_key_count"] == 0
    assert intake["operator_import"]["status"] == "not_required"
    assert intake["locally_stageable_key_count"] == 8
    assert intake["locally_staged_key_count"] == 8
    assert intake["locally_pending_key_count"] == 0
    assert intake["local_merge_review_required"] is True
    assert intake["local_merge_review_keys"] == sorted(
        review._LOCAL_CANDIDATE_KEYS
    )
    assert intake["local_merge_review_key_count"] == 8
    serialized = json.dumps(posture)
    for secret in (
        "base-signing-secret",
        "base-postgres-secret",
        "base-tunnel-secret",
        "database-role-secret",
        "layer-oauth-client",
        "layer-oauth-secret",
        "layer-state-secret",
        "layer-session-secret",
    ):
        assert secret not in serialized


def test_local_runtime_binding_candidate_detects_stale_or_unsafe_state(
    tmp_path: Path,
) -> None:
    root = _local_runtime_binding_root(tmp_path)
    candidate = Path(
        "_completion/propertyquarry_ooda_notification_cycle/"
        "local-runtime.env"
    )
    assert review.materialize_local_runtime_binding_candidate(
        root=root,
        candidate_path=candidate,
    )["status"] == "ready_for_merge_review"
    target = root / candidate
    target.write_text(
        "PROPERTYQUARRY_OODA_STAGE_UID=99999\n",
        encoding="utf-8",
    )
    stale = review.inspect_local_runtime_binding_candidate(
        root=root,
        candidate_path=candidate,
    )
    assert stale["status"] == "candidate_stale"
    assert stale["candidate_matches_current_derivation"] is False
    assert "99999" not in json.dumps(stale)

    target.chmod(0o640)
    unsafe = review.inspect_local_runtime_binding_candidate(
        root=root,
        candidate_path=candidate,
    )
    assert unsafe["status"] == "blocked"
    assert unsafe["file_status"] == "not_admissible"
    assert unsafe["configuration_merge_authorized"] is False


def test_local_runtime_binding_candidate_fails_closed_on_source_drift(
    tmp_path: Path,
) -> None:
    root = _local_runtime_binding_root(tmp_path)
    candidate = Path(
        "_completion/propertyquarry_ooda_notification_cycle/"
        "local-runtime.env"
    )
    assert review.materialize_local_runtime_binding_candidate(
        root=root,
        candidate_path=candidate,
    )["status"] == "ready_for_merge_review"
    (root / review._LOCAL_RUNTIME_BINDING_PATHS[
        "PROPERTYQUARRY_OODA_SCENE_SOURCE_DIR"
    ]).chmod(0o707)

    drifted = review.inspect_local_runtime_binding_candidate(
        root=root,
        candidate_path=candidate,
    )

    assert drifted["status"] == "blocked"
    assert drifted["blocking_reason"] == (
        "local_runtime_binding_sources_not_admissible"
    )
    assert drifted["source_directories_ready"] is False
    assert drifted["candidate_ready_for_merge_review"] is False
    assert drifted["configuration_merge_authorized"] is False


def test_runtime_observer_projects_health_without_dynamic_status_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "\n".join(
        [
            "propertyquarry-scheduler\trunning\tUp 4 seconds (health: starting)",
            "propertyquarry-api\trunning\tUp 2 hours (healthy)",
            "propertyquarry-worker\texited\tExited (1) 3 minutes ago",
        ]
    )
    monkeypatch.setattr(
        review,
        "_run",
        lambda *_args, **_kwargs: review.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=output,
            stderr="",
        ),
    )

    runtime = review._observe_runtime(now=NOW)

    assert runtime["services"] == [
        {
            "service": "propertyquarry-api",
            "state": "running",
            "health": "healthy",
        },
        {
            "service": "propertyquarry-scheduler",
            "state": "running",
            "health": "starting",
        },
        {
            "service": "propertyquarry-worker",
            "state": "exited",
            "health": "none",
        },
    ]


@pytest.mark.parametrize(
    (
        "services",
        "runtime_condition",
        "scheduler_condition",
        "scheduler_state",
        "scheduler_health",
    ),
    [
        (
            [{"service": "propertyquarry-api", "state": "running"}],
            "present",
            "absent",
            "",
            "none",
        ),
        (
            [{"service": "propertyquarry-scheduler", "state": "running"}],
            "present",
            "running",
            "running",
            "healthy",
        ),
        (
            [
                {
                    "service": "propertyquarry-scheduler",
                    "state": "running",
                    "health": "starting",
                }
            ],
            "present",
            "starting",
            "running",
            "starting",
        ),
        (
            [
                {
                    "service": "propertyquarry-scheduler",
                    "state": "running",
                    "health": "unhealthy",
                }
            ],
            "present",
            "unhealthy",
            "running",
            "unhealthy",
        ),
        (
            [{"service": "propertyquarry-scheduler", "state": "exited"}],
            "present",
            "not_running",
            "exited",
            "none",
        ),
        (
            [
                {"service": "propertyquarry-scheduler", "state": "exited"},
                {"service": "propertyquarry-scheduler", "state": "running"},
            ],
            "present",
            "ambiguous",
            "multiple",
            "multiple",
        ),
    ],
)
def test_runtime_observation_proves_scheduler_independently_of_project_presence(
    services: list[dict[str, str]],
    runtime_condition: str,
    scheduler_condition: str,
    scheduler_state: str,
    scheduler_health: str,
) -> None:
    receipt = review.build_runtime_observation_receipt(
        _runtime(services=services),
        now=NOW,
    )

    verification = review.verify_runtime_observation_receipt(receipt, now=NOW)

    assert verification["status"] == "verified"
    assert verification["runtime_condition"] == runtime_condition
    assert verification["scheduler_condition"] == scheduler_condition
    assert verification["scheduler_state"] == scheduler_state
    assert verification["scheduler_health"] == scheduler_health
    assert verification["scheduler_container_healthy"] is (
        scheduler_condition == "running"
    )
    assert verification["persistent_reevaluation_running"] is False
    assert verification["progress"]["scheduler_container_count"] == sum(
        row["service"] == "propertyquarry-scheduler" for row in services
    )
    assert verification["progress"]["running_scheduler_container_count"] == sum(
        row.get("service") == "propertyquarry-scheduler"
        and row.get("state") == "running"
        for row in services
    )
    assert verification["progress"]["healthy_scheduler_container_count"] == sum(
        row.get("service") == "propertyquarry-scheduler"
        and row.get("state") == "running"
        and row.get("health", "healthy") == "healthy"
        for row in services
    )
    assert verification["action_required"] is False
    assert verification["interrupt_operator"] is False
    assert verification["deployment_or_restart_authorized"] is False


def test_runtime_observation_bundle_is_private_current_and_drift_sensitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(review, "_observe_runtime", lambda **_kwargs: dict(runtime))
    observation_path = tmp_path / "runtime-observation.json"
    verification_path = tmp_path / "runtime-observation-verification.json"

    verified = review.materialize_current_runtime_observation_bundle(
        observation_path=observation_path,
        verification_path=verification_path,
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["progress"]["current_evidence_verified"] is True
    assert verified["verification_receipt_persisted"] is True
    assert stat.S_IMODE(observation_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600

    changed_runtime = _runtime(
        services=[{"service": "propertyquarry-api", "state": "running"}]
    )
    monkeypatch.setattr(
        review,
        "_observe_runtime",
        lambda **_kwargs: dict(changed_runtime),
    )
    drifted = review.verify_current_runtime_observation(
        observation_path=observation_path,
        now=NOW,
    )
    assert drifted["status"] == "blocked"
    assert (
        drifted["blocking_reason"]
        == "runtime_observation_current_binding_mismatch"
    )
    assert drifted["action_required"] is False
    assert drifted["interrupt_operator"] is False


def test_runtime_observation_rejects_stale_and_recomputed_contract_tamper() -> None:
    receipt = review.build_runtime_observation_receipt(_runtime(), now=NOW)

    stale = review.verify_runtime_observation_receipt(
        receipt,
        now=NOW + timedelta(seconds=1801),
    )
    assert stale["blocking_reason"] == "runtime_observation_not_fresh"

    receipt["deployment_or_restart_authorized"] = True
    normalized = dict(receipt)
    normalized.pop("integrity")
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": review._sha256(review._canonical(normalized)),
    }
    tampered = review.verify_runtime_observation_receipt(receipt, now=NOW)
    assert tampered["blocking_reason"] == "runtime_observation_contract_not_admissible"
    assert tampered["deployment_or_restart_authorized"] is False


def test_runtime_observation_cli_materializes_and_verifies_current_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    runtime = _runtime()

    def observed_runtime(**kwargs) -> dict[str, object]:
        return {
            **runtime,
            "observed_at": kwargs["now"].isoformat(),
        }

    monkeypatch.setattr(review, "_observe_runtime", observed_runtime)
    observation_path = tmp_path / "runtime-observation.json"
    verification_path = tmp_path / "runtime-observation-verification.json"

    exit_code = review.main(
        [
            "--observe-runtime",
            "--runtime-observation",
            str(observation_path),
            "--runtime-observation-verification-write",
            str(verification_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "verified"
    assert output["runtime_condition"] == "absent"
    assert output["scheduler_condition"] == "absent"
    assert output["scheduler_health"] == "none"
    assert output["scheduler_container_healthy"] is False
    assert output["persistent_reevaluation_running"] is False
    assert output["progress"]["current_evidence_verified"] is True
    assert output["verification_receipt_persisted"] is True
    assert stat.S_IMODE(observation_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600


def _probe() -> dict[str, object]:
    source_generated_at = (NOW - timedelta(minutes=10)).isoformat()
    values: dict[str, object] = {
        "source": "local_private_live_mobile_receipt",
        "source_sha256": "e" * 64,
        "source_bytes": 1406,
        "source_generated_at": source_generated_at,
        "status": "blocked",
        "base_origin": "http://127.0.0.1:8090",
        "host_header": "propertyquarry.com",
        "failure_code": "http_421_misdirected_request",
        "secret_values_recorded": False,
    }
    values["fingerprint_sha256"] = review._sha256(
        review._canonical(
            {
                "source_sha256": values["source_sha256"],
                "source_generated_at": values["source_generated_at"],
                "status": values["status"],
                "base_origin": values["base_origin"],
                "host_header": values["host_header"],
                "failure_code": values["failure_code"],
            }
        )
    )
    return values


def _edge_probe() -> dict[str, object]:
    source_generated_at = (NOW - timedelta(minutes=10)).isoformat()
    values: dict[str, object] = {
        "source": "public_https_live_mobile_receipt",
        "source_sha256": "7" * 64,
        "source_bytes": 2406,
        "source_generated_at": source_generated_at,
        "status": "blocked",
        "base_origin": "https://propertyquarry.com",
        "host_header": "propertyquarry.com",
        "failure_code": "cloudflare_1033_tunnel_unavailable",
        "edge_provider": "cloudflare",
        "edge_error_code": "1033",
        "http_status": 530,
        "failure_reason": "tunnel_unavailable",
        "secret_values_recorded": False,
    }
    values["fingerprint_sha256"] = review._sha256(
        review._canonical(review._probe_fingerprint_payload(values))
    )
    return values


def _release_authority() -> dict[str, object]:
    return {
        "source": "repository_release_manifest",
        "source_sha256": "f" * 64,
        "source_bytes": 4096,
        "release_repository": "ArchonMegalon/propertyquarry",
        "release_public_origin": "https://propertyquarry.com",
        "release_public_host": "propertyquarry.com",
        "release_deployment_id": "propertyquarry-governed-deploy-test",
        "release_generated_at": (NOW - timedelta(days=9)).isoformat(),
        "secret_values_recorded": False,
    }


def _packet() -> dict[str, object]:
    return review.build_review_packet(
        status=_operator_status(),
        snapshot_evidence=_source_evidence(),
        runtime_posture=_runtime(),
        release_posture=_release(),
        probe_posture=_probe(),
        release_authority=_release_authority(),
        now=NOW,
    )


def _edge_packet() -> dict[str, object]:
    return review.build_review_packet(
        status=_tunnel_operator_status(),
        snapshot_evidence=_source_evidence(),
        runtime_posture=_runtime(),
        release_posture=_release(),
        probe_posture=_edge_probe(),
        release_authority=_release_authority(),
        now=NOW,
    )


def test_review_packet_is_hash_bound_secret_free_and_non_executing() -> None:
    packet = _packet()

    verification = review.verify_review_packet(packet, now=NOW)

    assert verification["status"] == "verified"
    assert verification["execution_authorized"] is False
    assert verification["protected_operation_executed"] is False
    assert verification["provider_quota_consumption_allowed"] is False
    assert verification["delivery_authorized"] is False
    assert verification["configuration_proposal"]["protected_operations"] == [
        "runtime_configuration_change",
        "deployment_or_restart",
    ]
    assert packet["consent_gate"] == {
        "required": True,
        "authorization_recorded": False,
        "execution_authorized": False,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
    }
    assert packet["release_posture"]["changed_paths_recorded"] is False
    proposal = packet["configuration_proposal"]
    assert proposal["probe_target_change_proposed"] is True
    assert proposal["current_probe_origin"] == "http://127.0.0.1:8090"
    assert proposal["proposed_probe_origin"] == "http://127.0.0.1:8097"
    assert proposal["dedicated_runtime_present"] is False
    assert "dedicated_runtime_services_absent" in proposal["findings"]
    assert packet["progress"]["review_item_count"] == 5
    assert packet["secret_values_recorded"] is False
    assert "DATABASE_URL" not in json.dumps(packet)


def test_tunnel_review_is_normalized_current_and_non_executing() -> None:
    packet = _edge_packet()

    verification = review.verify_review_packet(packet, now=NOW)

    assert verification["status"] == "verified"
    assert verification["blocking_reason"] == "live_runtime_tunnel_unavailable"
    assert verification["configuration_proposal"]["incident"] == {
        "kind": "edge_connector_unavailable",
        "provider": "cloudflare",
        "code": "1033",
        "http_status": 530,
        "reason": "tunnel_unavailable",
        "source": "public_https_probe",
    }
    proposal = packet["configuration_proposal"]
    assert proposal["current_probe_origin"] == "https://propertyquarry.com"
    assert proposal["proposed_probe_origin"] == "https://propertyquarry.com"
    assert proposal["probe_target_change_proposed"] is False
    assert proposal["probe_host_change_proposed"] is False
    assert "public_edge_tunnel_connector_unavailable" in proposal["findings"]
    assert "dedicated_runtime_services_absent" in proposal["findings"]
    preview = proposal["recovery_preview"]
    assert preview["operation"] == "deployment_or_restart"
    assert preview["compose_project"] == "property"
    assert preview["compose_files"] == [
        "docker-compose.property.yml",
        "docker-compose.cloudflared.yml",
    ]
    assert preview["connector_service"] == "propertyquarry-cloudflared"
    assert preview["application_service"] == "propertyquarry-api"
    assert len(preview["service_targets"]) == len(
        review.local_deployment.SERVICE_CONTRACT
    )
    assert all(
        row["observed_state"] == "absent" for row in preview["service_targets"]
    )
    assert preview["preview_only"] is True
    assert preview["command"] == review._compose_command("start")
    assert preview["rollback_command"] == review._compose_command(
        "rollback_to_absent"
    )
    assert preview["source_binding"] == {
        "runtime_fingerprint_sha256": packet["runtime_posture"][
            "fingerprint_sha256"
        ],
        "release_head_sha": packet["release_posture"]["head_sha"],
        "release_worktree_fingerprint_sha256": packet["release_posture"][
            "worktree_fingerprint_sha256"
        ],
    }
    assert preview["pre_state"] == "all_required_services_absent"
    assert preview["reversible_to_observed_state"] is True
    environment = preview["deployment_environment"]
    expected_blockers = ["deployment_environment_authority_not_verified"]
    if environment["configuration_complete"] is not True:
        expected_blockers.insert(0, "deployment_environment_not_ready")
    expected_blockers.append("release_worktree_not_clean")
    assert preview["blocking_reasons"] == expected_blockers
    assert review._deployment_environment_posture_admissible(environment)
    assert environment["environment_values_recorded"] is False
    assert environment["environment_values_hashed"] is False
    assert environment["secret_values_recorded"] is False
    assert preview["deployment_environment_values_recorded"] is False
    assert preview["deployment_environment_authority_verified"] is False
    assert preview["eligible_for_authorization"] is False
    assert preview["command_recorded"] is True
    assert preview["rollback_command_recorded"] is True
    assert preview["authorization_recorded"] is False
    assert preview["execution_authorized"] is False
    assert preview["deployment_or_restart_performed"] is False
    assert [row["id"] for row in packet["review_items"]] == [
        "public_probe",
        "edge_connector",
        "deployment_environment_intake",
        "dedicated_runtime",
        "release_worktree",
    ]
    assert packet["consent_gate"]["authorization_recorded"] is False
    assert packet["automatic_execution_allowed"] is False
    assert packet["execution_authorized"] is False
    assert packet["protected_operation_executed"] is False
    assert packet["provider_quota_consumption_allowed"] is False
    assert packet["delivery_authorized"] is False


def test_tunnel_presentation_context_binds_incident_without_granting_authority() -> None:
    verification = review.verify_review_packet(_edge_packet(), now=NOW)
    verification["progress"]["current_evidence_verified"] = True
    recovery_preview = verification["configuration_proposal"]["recovery_preview"]
    intake = recovery_preview["deployment_environment"]["intake_plan"]

    context = review.operator_presentation_context(verification)

    assert context == {
        "schema": "propertyquarry.ooda_operator_presentation_context.v1",
        "lane": "gold_live_runtime",
        "scope": {
            "operation": "deployment_or_restart_review",
            "change_id": "propertyquarry_edge_connector_recovery",
            "edge_provider": "cloudflare",
            "edge_error_code": "1033",
            "http_status": 530,
            "failure_reason": "tunnel_unavailable",
            "recovery_preview_sha256": review._sha256(
                review._canonical(recovery_preview)
            ),
            **(
                {"consent_request": verification["consent_request"]}
                if verification["consent_request"].get("request_id")
                else {}
            ),
            "deployment_environment_intake": {
                "status": intake["status"],
                "blocking_reason": intake["blocking_reason"],
                "next_action": intake["next_action"],
                "operator_external_input_required": intake[
                    "operator_external_input_required"
                ],
                "operator_external_input_keys": intake[
                    "operator_external_input_keys"
                ],
                "operator_external_input_key_count": intake[
                    "operator_external_input_key_count"
                ],
                "operator_import_path": intake["operator_import"]["path"],
                "operator_import_status": intake["operator_import"]["status"],
                "operator_import_file_status": intake["operator_import"][
                    "file_status"
                ],
                "operator_import_ready_for_review": intake["operator_import"][
                    "import_ready_for_review"
                ],
                "operator_merge_review_required": intake[
                    "operator_merge_review_required"
                ],
                "operator_merge_review_keys": intake[
                    "operator_merge_review_keys"
                ],
                "operator_merge_review_key_count": intake[
                    "operator_merge_review_key_count"
                ],
                "locally_stageable_key_count": intake[
                    "locally_stageable_key_count"
                ],
                "locally_staged_key_count": intake[
                    "locally_staged_key_count"
                ],
                    "locally_pending_key_count": intake[
                        "locally_pending_key_count"
                    ],
                    "local_merge_review_required": intake[
                        "local_merge_review_required"
                    ],
                    "local_merge_review_keys": intake[
                        "local_merge_review_keys"
                    ],
                    "local_merge_review_key_count": intake[
                        "local_merge_review_key_count"
                    ],
                "local_runtime_candidate_path": intake[
                    "local_runtime_binding_candidate"
                ]["path"],
                "local_runtime_candidate_status": intake[
                    "local_runtime_binding_candidate"
                ]["status"],
                "local_runtime_candidate_ready_for_merge_review": intake[
                    "local_runtime_binding_candidate"
                ]["candidate_ready_for_merge_review"],
                "local_runtime_candidate_values_hashed": False,
                "configuration_merge_authorized": False,
                "configuration_merged": False,
                "configuration_apply_authorized": False,
                "deployment_or_restart_authorized": False,
                "environment_values_recorded": False,
                "secret_values_recorded": False,
            },
            "release_public_origin": "https://propertyquarry.com",
            "compose_project": "property",
            "protected_operations": [
                "runtime_configuration_change",
                "deployment_or_restart",
            ],
        },
    }

    tampered = json.loads(json.dumps(verification))
    tampered["configuration_proposal"]["incident"]["code"] = "1034"
    with pytest.raises(ValueError, match="runtime_review_not_admissible"):
        review.operator_presentation_context(tampered)


def test_operator_presentation_context_excludes_receipt_churn_but_binds_scope() -> None:
    verification = review.verify_review_packet(_packet(), now=NOW)
    verification["progress"]["current_evidence_verified"] = True

    context = review.operator_presentation_context(verification)

    refreshed = json.loads(json.dumps(verification))
    refreshed["updated_at"] = (NOW + timedelta(minutes=1)).isoformat()
    refreshed["packet_sha256"] = "9" * 64
    refreshed["configuration_proposal"]["missing_service_count"] = 0
    assert review.operator_presentation_context(refreshed) == context

    changed = json.loads(json.dumps(verification))
    changed["configuration_proposal"]["current_probe_origin"] = (
        "http://127.0.0.1:8091"
    )
    assert review.operator_presentation_context(changed) != context
    assert context["scope"] == {
        "operation": "runtime_configuration_change",
        "change_id": "propertyquarry_local_probe_origin",
        "current_probe_origin": "http://127.0.0.1:8090",
        "proposed_probe_origin": "http://127.0.0.1:8097",
        "current_probe_host": "propertyquarry.com",
        "proposed_probe_host": "propertyquarry.com",
        "release_public_origin": "https://propertyquarry.com",
        "compose_project": "property",
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
    }


def test_review_packet_rejects_integrity_tamper_and_recomputed_contract_tamper() -> None:
    packet = _packet()
    packet["review_items"][0]["question"] = "deploy it immediately"

    integrity_failure = review.verify_review_packet(packet, now=NOW)
    assert integrity_failure["blocking_reason"] == "review_packet_integrity_invalid"

    normalized = dict(packet)
    normalized.pop("integrity")
    packet["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": review._sha256(review._canonical(normalized)),
    }
    contract_failure = review.verify_review_packet(packet, now=NOW)
    assert contract_failure["blocking_reason"] == "review_packet_contract_not_admissible"
    assert contract_failure["execution_authorized"] is False


def test_review_packet_rejects_stale_or_implicitly_authorized_evidence() -> None:
    packet = _packet()
    stale = review.verify_review_packet(
        packet,
        now=NOW + timedelta(seconds=1801),
    )
    assert stale["blocking_reason"] == "review_packet_not_fresh"

    status = _operator_status()
    status["actions"][0]["automatic_execution_allowed"] = True
    with pytest.raises(ValueError, match="operator_action_not_admissible"):
        review.build_review_packet(
            status=status,
            snapshot_evidence=_source_evidence(),
            runtime_posture=_runtime(),
            release_posture=_release(),
            probe_posture=_probe(),
            release_authority=_release_authority(),
            now=NOW,
        )


def test_probe_and_release_sources_are_strictly_projected_without_secrets(
    tmp_path: Path,
) -> None:
    source_generated_at = (NOW - timedelta(minutes=10)).isoformat()
    live_path = tmp_path / "live-mobile.json"
    live_path.write_text(
        json.dumps(
            {
                "status": "blocked",
                "generated_at": source_generated_at,
                "base_url": "http://127.0.0.1:8090",
                "host_header": "propertyquarry.com",
                "error": "HTTP Error 421: Misdirected Request",
                "api_token": "must-not-be-projected",
            }
        ),
        encoding="utf-8",
    )
    live_path.chmod(0o600)
    probe = review._live_mobile_probe_posture(
        receipt_path=live_path,
        expected_source_generated_at=source_generated_at,
        now=NOW,
    )

    assert probe["base_origin"] == "http://127.0.0.1:8090"
    assert probe["failure_code"] == "http_421_misdirected_request"
    assert probe["secret_values_recorded"] is False
    assert "must-not-be-projected" not in json.dumps(probe)

    edge_failure = {
        "provider": "cloudflare",
        "code": "1033",
        "reason": "cloudflare_tunnel_unavailable",
        "http_status": 530,
    }
    edge_path = tmp_path / "public-edge.json"
    edge_payload = {
        "status": "blocked",
        "generated_at": source_generated_at,
        "base_url": "https://propertyquarry.com",
        "host_header": "",
        "route_count": 1,
        "error": "cloudflare_error_1033_tunnel_unavailable",
        "edge_failure": edge_failure,
        "routes": [
            {
                "status_code": 530,
                "ok": False,
                "metrics": {"edge_failure": edge_failure},
            }
        ],
        "response_body": "must-not-be-projected",
    }
    edge_path.write_text(json.dumps(edge_payload), encoding="utf-8")
    edge_path.chmod(0o600)

    edge_probe = review._live_mobile_probe_posture(
        receipt_path=edge_path,
        expected_source_generated_at=source_generated_at,
        now=NOW,
    )

    assert edge_probe["source"] == "public_https_live_mobile_receipt"
    assert edge_probe["base_origin"] == "https://propertyquarry.com"
    assert edge_probe["host_header"] == "propertyquarry.com"
    assert edge_probe["edge_provider"] == "cloudflare"
    assert edge_probe["edge_error_code"] == "1033"
    assert edge_probe["http_status"] == 530
    assert edge_probe["failure_reason"] == "tunnel_unavailable"
    assert "must-not-be-projected" not in json.dumps(edge_probe)

    observation_path = tmp_path / "public-origin-observation.json"
    observation_path.write_text(
        json.dumps(
            {
                "schema": "propertyquarry.ooda_public_origin_observation.v1",
                "generated_at": source_generated_at,
                "status": "blocked",
                "origin": "https://propertyquarry.com",
                "observation": {
                    "edge_provider": "cloudflare",
                    "error_code": 1033,
                    "http_status": 530,
                    "reason": "cloudflare_tunnel_unavailable",
                },
                "request": {
                    "credentials_sent": False,
                    "method": "GET",
                    "path": "/",
                    "redirects_followed": False,
                    "tls_validation": "system_trust_store",
                },
                "response_headers_recorded": False,
                "response_content_recorded": False,
                "action_required": False,
                "interrupt_operator": False,
                "automatic_execution_allowed": False,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
                "protected_operation_executed": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
                "delivery_attempted": False,
                "sent": False,
            }
        ),
        encoding="utf-8",
    )
    observation_path.chmod(0o600)

    observation_probe = review._live_mobile_probe_posture(
        receipt_path=observation_path,
        expected_source_generated_at=source_generated_at,
        now=NOW,
    )

    assert observation_probe["source"] == "public_https_origin_observation"
    assert observation_probe["base_origin"] == "https://propertyquarry.com"
    assert observation_probe["host_header"] == "propertyquarry.com"
    assert observation_probe["edge_provider"] == "cloudflare"
    assert observation_probe["edge_error_code"] == "1033"
    assert observation_probe["http_status"] == 530
    assert observation_probe["failure_reason"] == "tunnel_unavailable"
    assert observation_probe["secret_values_recorded"] is False

    edge_payload["edge_failure"] = {**edge_failure, "provider": "example"}
    edge_path.write_text(json.dumps(edge_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="live_mobile_receipt_not_admissible"):
        review._live_mobile_probe_posture(
            receipt_path=edge_path,
            expected_source_generated_at=source_generated_at,
            now=NOW,
        )

    manifest_path = tmp_path / "release.md"
    manifest_values = {
        "release_repository": "ArchonMegalon/propertyquarry",
        "release_public_origin": "https://propertyquarry.com",
        "release_deployment_id": "propertyquarry-governed-deploy-test",
        "release_generated_at": (NOW - timedelta(days=9)).isoformat(),
    }
    manifest_path.write_text(
        "release\n"
        + review.launch_room.MANIFEST_START
        + "\n```json\n"
        + json.dumps(manifest_values)
        + "\n```\n"
        + review.launch_room.MANIFEST_END
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o644)
    authority = review._release_authority_posture(
        manifest_path=manifest_path,
        now=NOW,
    )

    assert authority["release_public_origin"] == "https://propertyquarry.com"
    assert authority["release_public_host"] == "propertyquarry.com"
    assert authority["secret_values_recorded"] is False

    live_path.chmod(0o640)
    with pytest.raises(ValueError, match="live_mobile_receipt_not_admissible"):
        review._live_mobile_probe_posture(
            receipt_path=live_path,
            expected_source_generated_at=source_generated_at,
            now=NOW,
        )


def test_current_review_verifier_detects_runtime_or_release_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    release = _release()
    monkeypatch.setattr(
        review.operator_status,
        "load_operator_status",
        lambda **_kwargs: _operator_status(),
    )
    monkeypatch.setattr(
        review,
        "_snapshot_evidence",
        lambda **_kwargs: _source_evidence(),
    )
    monkeypatch.setattr(review, "_observe_runtime", lambda **_kwargs: dict(runtime))
    monkeypatch.setattr(review, "_release_posture", lambda **_kwargs: dict(release))
    monkeypatch.setattr(
        review,
        "_live_mobile_probe_posture",
        lambda **_kwargs: _probe(),
    )
    monkeypatch.setattr(
        review,
        "_release_authority_posture",
        lambda **_kwargs: _release_authority(),
    )
    packet_path = tmp_path / "runtime-review.json"

    review.materialize_current_review_packet(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        write_path=packet_path,
        now=NOW,
    )
    verified = review.verify_current_review_packet(
        packet_path=packet_path,
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        now=NOW,
    )

    assert verified["status"] == "verified"
    assert verified["progress"]["current_evidence_verified"] is True
    assert stat.S_IMODE(packet_path.stat().st_mode) == 0o600

    changed_runtime = _runtime(
        services=[{"service": "propertyquarry-api", "state": "running"}]
    )
    monkeypatch.setattr(
        review,
        "_observe_runtime",
        lambda **_kwargs: changed_runtime,
    )
    drifted = review.verify_current_review_packet(
        packet_path=packet_path,
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        now=NOW,
    )
    assert drifted["status"] == "blocked"
    assert drifted["blocking_reason"] == "review_packet_current_binding_mismatch"


def test_current_tunnel_review_detects_deployment_environment_presence_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    release = _release()
    monkeypatch.setattr(
        review.operator_status,
        "load_operator_status",
        lambda **_kwargs: _tunnel_operator_status(),
    )
    monkeypatch.setattr(
        review,
        "_snapshot_evidence",
        lambda **_kwargs: _source_evidence(),
    )
    monkeypatch.setattr(review, "_observe_runtime", lambda **_kwargs: dict(runtime))
    monkeypatch.setattr(review, "_release_posture", lambda **_kwargs: dict(release))
    monkeypatch.setattr(
        review,
        "_live_mobile_probe_posture",
        lambda **_kwargs: _edge_probe(),
    )
    monkeypatch.setattr(
        review,
        "_release_authority_posture",
        lambda **_kwargs: _release_authority(),
    )
    monkeypatch.setattr(
        review,
        "_deployment_environment_posture_admissible",
        lambda _posture: True,
    )
    states = [
        {"presence_state": "incomplete", "secret_values_recorded": False},
        {"presence_state": "complete", "secret_values_recorded": False},
    ]
    monkeypatch.setattr(
        review,
        "inspect_deployment_environment",
        lambda **_kwargs: dict(states.pop(0)),
    )
    packet_path = tmp_path / "runtime-review.json"

    review.materialize_current_review_packet(
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        write_path=packet_path,
        now=NOW,
    )
    drifted = review.verify_current_review_packet(
        packet_path=packet_path,
        cycle_receipt_path=tmp_path / "cycle.json",
        signal_dir=tmp_path / "signals",
        now=NOW,
    )

    assert drifted["status"] == "blocked"
    assert drifted["blocking_reason"] == "review_packet_current_binding_mismatch"


def test_cli_overwrites_success_verification_receipt_with_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    packet_path = tmp_path / "packet.json"
    verification_path = tmp_path / "verification.json"
    monkeypatch.setattr(
        review,
        "materialize_current_review_packet",
        lambda **_kwargs: {"generated_at": NOW.isoformat()},
    )
    monkeypatch.setattr(
        review,
        "verify_current_review_packet",
        lambda **_kwargs: {
            "schema": review.VERIFY_SCHEMA,
            "status": "verified",
            "updated_at": NOW.isoformat(),
            "blocking_reason": "live_runtime_host_admission_rejected",
            "next_action": "review only",
            "progress": {"current_evidence_verified": True},
            "execution_authorized": False,
            "protected_operation_executed": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        },
    )

    success = review.main(
        [
            "--packet",
            str(packet_path),
            "--verification-write",
            str(verification_path),
        ]
    )
    success_receipt = json.loads(verification_path.read_text(encoding="utf-8"))
    assert success == 0
    assert success_receipt["status"] == "verified"
    assert success_receipt["verification_receipt_persisted"] is True
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    capsys.readouterr()

    monkeypatch.setattr(
        review,
        "materialize_current_review_packet",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("must-not-be-recorded")),
    )
    failure = review.main(
        [
            "--packet",
            str(packet_path),
            "--verification-write",
            str(verification_path),
        ]
    )
    failure_receipt = json.loads(verification_path.read_text(encoding="utf-8"))

    assert failure == 1
    assert failure_receipt["status"] == "blocked"
    assert failure_receipt["blocking_reason"] == "runtime_review_materialization_failed"
    assert failure_receipt["verification_receipt_persisted"] is True
    assert "must-not-be-recorded" not in json.dumps(failure_receipt)
