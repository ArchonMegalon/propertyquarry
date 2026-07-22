from __future__ import annotations

import uuid

from tests.smoke_runtime_api_support import build_client as _client
from tests.smoke_runtime_api_support import build_headers as _headers
from tests.product_test_helpers import start_workspace


def test_health_ready_and_version() -> None:
    client = _client(storage_backend="memory")
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/health/live").json()["status"] == "live"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    version = client.get("/version")
    assert version.status_code == 200
    version_body = version.json()
    assert version_body["app_name"]
    assert version_body["version"]
    assert version_body["property_search_run_retention_status"] == "enabled"
    assert version_body["property_search_run_retention_seconds"] == "7776000"
    assert version_body["property_search_run_retention_days"] == "90.0"
    assert version_body["id_austria_sign_in_status"] in {
        "disabled",
        "dry_verified_configured",
        "blocked_missing_configuration",
    }
    assert version_body["id_austria_sign_in_configured"] in {"true", "false"}
    assert "id_austria_sign_in_missing_env" in version_body


def test_version_exposes_authoritative_propertyquarry_release_manifest(monkeypatch) -> None:
    override_fields = {
        "PROPERTYQUARRY_RELEASE_REPOSITORY": "release_repository",
        "PROPERTYQUARRY_RELEASE_BRANCH": "release_branch",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": "release_commit_sha",
        "PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID": "release_deployment_id",
        "PROPERTYQUARRY_RELEASE_PUBLIC_ORIGIN": "release_public_origin",
        "PROPERTYQUARRY_RELEASE_ARTIFACT_SET": "release_artifact_set",
        "PROPERTYQUARRY_RELEASE_LABEL": "release_label",
        "PROPERTYQUARRY_RELEASE_GENERATED_AT": "release_generated_at",
    }
    for env_name in override_fields:
        monkeypatch.delenv(env_name, raising=False)

    baseline = _client(storage_backend="memory").get("/version").json()
    assert baseline["release_manifest_status"] == "complete"
    for env_name, field_name in override_fields.items():
        monkeypatch.setenv(env_name, baseline[field_name])

    version = _client(storage_backend="memory").get("/version")

    assert version.status_code == 200
    body = version.json()
    assert body["release_manifest_status"] == "complete"
    for field_name in override_fields.values():
        assert body[field_name] == baseline[field_name]
    assert body["release_repository"] == "ArchonMegalon/propertyquarry"
    assert body["release_artifact_set"].startswith(
        "propertyquarry-generated-release-artifacts-v1@sha256:"
    )
    assert "propertyquarry-web-runtime" not in body["release_artifact_set"]
    assert "propertyquarry-scheduler" not in body["release_artifact_set"]


def test_rewrite_and_policy_audit_flow() -> None:
    client = _client(storage_backend="memory")
    create = client.post("/v1/rewrite/artifact", json={"text": "smoke"})
    assert create.status_code == 200
    payload = create.json()
    artifact_id = payload["artifact_id"]
    session_id = payload["execution_session_id"]
    assert payload["principal_id"] == "exec-1"

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    body = session.json()
    event_names = [e["name"] for e in body["events"]]
    assert "plan_compiled" in event_names
    assert "policy_decision" in event_names
    assert "input_prepared" in event_names
    assert "policy_step_completed" in event_names
    assert "tool_execution_completed" in event_names
    assert event_names.index("input_prepared") < event_names.index("policy_decision") < event_names.index(
        "policy_step_completed"
    )
    assert "step_enqueued" in event_names
    assert "queue_item_completed" in event_names
    assert len(body["steps"]) >= 3
    assert body["steps"][0]["input_json"]["plan_step_key"] == "step_input_prepare"
    assert body["steps"][0]["input_json"]["owner"] == "system"
    assert body["steps"][0]["input_json"]["authority_class"] == "observe"
    assert body["steps"][0]["input_json"]["review_class"] == "none"
    assert body["steps"][0]["input_json"]["failure_strategy"] == "fail"
    assert body["steps"][0]["input_json"]["timeout_budget_seconds"] == 30
    assert body["steps"][0]["input_json"]["max_attempts"] == 1
    assert body["steps"][0]["input_json"]["retry_backoff_seconds"] == 0
    assert body["steps"][1]["input_json"]["plan_step_key"] == "step_policy_evaluate"
    assert body["steps"][1]["input_json"]["owner"] == "system"
    assert body["steps"][1]["input_json"]["authority_class"] == "observe"
    assert body["steps"][2]["input_json"]["plan_step_key"] == "step_artifact_save"
    assert body["steps"][2]["input_json"]["owner"] == "tool"
    assert body["steps"][2]["input_json"]["authority_class"] == "draft"
    assert body["steps"][2]["input_json"]["timeout_budget_seconds"] == 60
    steps_by_key = {
        step["input_json"]["plan_step_key"]: step
        for step in body["steps"]
    }
    assert steps_by_key["step_input_prepare"]["dependency_keys"] == []
    assert steps_by_key["step_input_prepare"]["dependency_states"] == {}
    assert steps_by_key["step_input_prepare"]["dependency_step_ids"] == {}
    assert steps_by_key["step_input_prepare"]["blocked_dependency_keys"] == []
    assert steps_by_key["step_input_prepare"]["dependencies_satisfied"] is True
    assert steps_by_key["step_input_prepare"]["parent_step_id"] is None
    assert steps_by_key["step_policy_evaluate"]["dependency_keys"] == ["step_input_prepare"]
    assert steps_by_key["step_policy_evaluate"]["parent_step_id"] == steps_by_key["step_input_prepare"]["step_id"]
    assert steps_by_key["step_policy_evaluate"]["dependency_states"] == {"step_input_prepare": "completed"}
    assert (
        steps_by_key["step_policy_evaluate"]["dependency_step_ids"]["step_input_prepare"]
        == steps_by_key["step_input_prepare"]["step_id"]
    )
    assert steps_by_key["step_policy_evaluate"]["blocked_dependency_keys"] == []
    assert steps_by_key["step_policy_evaluate"]["dependencies_satisfied"] is True
    assert steps_by_key["step_artifact_save"]["dependency_keys"] == ["step_policy_evaluate"]
    assert steps_by_key["step_artifact_save"]["parent_step_id"] == steps_by_key["step_policy_evaluate"]["step_id"]
    assert steps_by_key["step_artifact_save"]["dependency_states"] == {"step_policy_evaluate": "completed"}
    assert (
        steps_by_key["step_artifact_save"]["dependency_step_ids"]["step_policy_evaluate"]
        == steps_by_key["step_policy_evaluate"]["step_id"]
    )
    assert steps_by_key["step_artifact_save"]["blocked_dependency_keys"] == []
    assert steps_by_key["step_artifact_save"]["dependencies_satisfied"] is True
    assert body["human_task_assignment_history"] == []
    assert all(step["state"] in {"completed", "running", "blocked", "waiting_approval", "queued"} for step in body["steps"])
    assert len(body["queue_items"]) >= 3
    assert all(item["state"] == "done" for item in body["queue_items"])
    assert len(body["receipts"]) >= 1
    receipt_id = body["receipts"][0]["receipt_id"]
    assert body["artifacts"][0]["artifact_id"] == payload["artifact_id"]
    assert body["artifacts"][0]["task_key"] == "rewrite_text"
    assert body["artifacts"][0]["deliverable_type"] == "rewrite_note"
    assert body["artifacts"][0]["principal_id"] == "exec-1"
    assert body["artifacts"][0]["mime_type"] == "text/plain"
    assert body["artifacts"][0]["preview_text"] == "smoke"
    assert body["artifacts"][0]["storage_handle"] == f"artifact://{artifact_id}"
    assert body["artifacts"][0]["body_ref"].startswith("artifact://")
    assert body["artifacts"][0]["structured_output_json"] == {}
    assert body["artifacts"][0]["attachments_json"] == {}
    assert len(body["run_costs"]) >= 1
    cost_id = body["run_costs"][0]["cost_id"]

    fetched_artifact = client.get(f"/v1/rewrite/artifacts/{artifact_id}")
    assert fetched_artifact.status_code == 200
    assert fetched_artifact.json()["artifact_id"] == artifact_id
    assert fetched_artifact.json()["execution_session_id"] == session_id
    assert fetched_artifact.json()["content"] == "smoke"
    assert fetched_artifact.json()["principal_id"] == "exec-1"
    assert fetched_artifact.json()["mime_type"] == "text/plain"
    assert fetched_artifact.json()["preview_text"] == "smoke"
    assert fetched_artifact.json()["storage_handle"] == f"artifact://{artifact_id}"
    assert fetched_artifact.json()["body_ref"].startswith("artifact://")
    assert fetched_artifact.json()["structured_output_json"] == {}
    assert fetched_artifact.json()["attachments_json"] == {}
    assert fetched_artifact.json()["task_key"] == "rewrite_text"
    assert fetched_artifact.json()["deliverable_type"] == "rewrite_note"

    fetched_receipt = client.get(f"/v1/rewrite/receipts/{receipt_id}")
    assert fetched_receipt.status_code == 200
    assert fetched_receipt.json()["receipt_id"] == receipt_id
    assert fetched_receipt.json()["target_ref"] == artifact_id
    assert fetched_receipt.json()["receipt_json"]["handler_key"] == "artifact_repository"
    assert fetched_receipt.json()["receipt_json"]["invocation_contract"] == "tool.v1"
    assert fetched_receipt.json()["task_key"] == "rewrite_text"
    assert fetched_receipt.json()["deliverable_type"] == "rewrite_note"

    fetched_cost = client.get(f"/v1/rewrite/run-costs/{cost_id}")
    assert fetched_cost.status_code == 200
    assert fetched_cost.json()["cost_id"] == cost_id
    assert fetched_cost.json()["model_name"] == "none"
    assert fetched_cost.json()["task_key"] == "rewrite_text"
    assert fetched_cost.json()["deliverable_type"] == "rewrite_note"

    policy = client.get("/v1/policy/decisions/recent", params={"session_id": session_id, "limit": 5})
    assert policy.status_code == 200
    decisions = policy.json()
    assert len(decisions) >= 1
    assert decisions[0]["reason"] == "allowed"

    missing_artifact = client.get("/v1/rewrite/artifacts/not-a-real-artifact-id")
    assert missing_artifact.status_code == 404
    assert missing_artifact.json()["error"]["code"] == "artifact_not_found"


def test_rewrite_routes_enforce_principal_scope() -> None:
    client = _client(storage_backend="memory", principal_id="exec-1")

    create = client.post(
        "/v1/rewrite/artifact",
        json={"text": "principal scoped rewrite", "principal_id": "exec-1"},
    )
    assert create.status_code == 200
    artifact_id = create.json()["artifact_id"]
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    body = session.json()
    receipt_id = body["receipts"][0]["receipt_id"]
    cost_id = body["run_costs"][0]["cost_id"]

    mismatch_headers = _headers(principal_id="exec-2")
    for path in (
        f"/v1/rewrite/sessions/{session_id}",
        f"/v1/rewrite/artifacts/{artifact_id}",
        f"/v1/rewrite/receipts/{receipt_id}",
        f"/v1/rewrite/run-costs/{cost_id}",
    ):
        mismatch = client.get(path, headers=mismatch_headers)
        assert mismatch.status_code == 403
        assert mismatch.json()["error"]["code"] == "principal_scope_mismatch"

    create_mismatch = client.post(
        "/v1/rewrite/artifact",
        headers=_headers(principal_id="exec-1"),
        json={"text": "principal mismatch", "principal_id": "exec-2"},
    )
    assert create_mismatch.status_code == 403
    assert create_mismatch.json()["error"]["code"] == "principal_scope_mismatch"

    missing_receipt = client.get("/v1/rewrite/receipts/not-a-real-receipt-id")
    assert missing_receipt.status_code == 404
    assert missing_receipt.json()["error"]["code"] == "receipt_not_found"

    missing_cost = client.get("/v1/rewrite/run-costs/not-a-real-cost-id")
    assert missing_cost.status_code == 404
    assert missing_cost.json()["error"]["code"] == "run_cost_not_found"


def test_tool_execute_rejects_foreign_principal_payload_mismatch() -> None:
    client = _client(storage_backend="memory", principal_id="exec-tool-exec", operator=True)
    binding_json = client.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "gmail",
            "external_account_ref": "tool-exec-acct",
            "scope_json": {"scopes": ["mail.send"]},
            "auth_metadata_json": {"provider": "google"},
            "status": "enabled",
        },
    )
    assert binding_json.status_code == 200
    binding_id = binding_json.json()["binding_id"]

    execute_mismatch = client.post(
        "/v1/tools/execute",
        json={
            "tool_name": "connector.dispatch",
            "action_kind": "delivery.send",
            "payload_json": {
                "principal_id": "exec-tool-mismatch",
                "binding_id": binding_id,
                "channel": "email",
                "recipient": "ops+tool@example.com",
                "content": "scope mismatch smoke payload",
                "idempotency_key": "scope-mismatch-tool-execute",
            },
        },
    )
    assert execute_mismatch.status_code == 403
    assert execute_mismatch.json()["error"]["code"] == "principal_scope_mismatch"


def test_human_task_session_routes_enforce_session_principal_scope() -> None:
    client = _client(storage_backend="memory", principal_id="exec-1")

    create = client.post("/v1/rewrite/artifact", json={"text": "human task session scope"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]

    create_mismatch = client.post(
        "/v1/human/tasks",
        headers=_headers(principal_id="exec-2"),
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "cross-principal attach attempt",
        },
    )
    assert create_mismatch.status_code == 403
    assert create_mismatch.json()["error"]["code"] == "principal_scope_mismatch"

    list_mismatch = client.get(
        "/v1/human/tasks",
        headers=_headers(principal_id="exec-2"),
        params={"session_id": session_id, "limit": 10},
    )
    assert list_mismatch.status_code == 403
    assert list_mismatch.json()["error"]["code"] == "principal_scope_mismatch"

    listed = client.get("/v1/human/tasks", params={"session_id": session_id, "limit": 10})
    assert listed.status_code == 200
    assert listed.json() == []


def test_rewrite_requires_approval_then_approve_flow() -> None:
    client = _client(storage_backend="memory", approval_threshold_chars=5)
    create = client.post("/v1/rewrite/artifact", json={"text": "approval smoke payload"})
    assert create.status_code == 202
    assert create.json()["status"] == "awaiting_approval"
    assert create.json()["next_action"] == "poll_or_subscribe"

    pending = client.get("/v1/policy/approvals/pending", params={"limit": 10})
    assert pending.status_code == 200
    rows = pending.json()
    assert len(rows) >= 1
    approval_id = create.json()["approval_id"]
    session_id = create.json()["session_id"]
    assert any(row["approval_id"] == approval_id and row["session_id"] == session_id for row in rows)
    assert rows[0]["status"] == "pending"

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    body = session.json()
    assert body["status"] == "awaiting_approval"
    assert len(body["artifacts"]) == 0
    assert len(body["queue_items"]) >= 2
    assert [item["state"] for item in body["queue_items"][:2]] == ["done", "done"]
    assert len(body["receipts"]) == 0
    approval_steps = {
        step["input_json"]["plan_step_key"]: step
        for step in body["steps"]
    }
    assert approval_steps["step_input_prepare"]["state"] == "completed"
    assert approval_steps["step_policy_evaluate"]["state"] == "completed"
    assert approval_steps["step_policy_evaluate"]["dependency_states"] == {"step_input_prepare": "completed"}
    assert approval_steps["step_policy_evaluate"]["blocked_dependency_keys"] == []
    assert approval_steps["step_policy_evaluate"]["dependencies_satisfied"] is True
    assert approval_steps["step_artifact_save"]["state"] == "waiting_approval"
    assert approval_steps["step_artifact_save"]["dependency_keys"] == ["step_policy_evaluate"]
    assert approval_steps["step_artifact_save"]["dependency_states"] == {"step_policy_evaluate": "completed"}
    assert (
        approval_steps["step_artifact_save"]["dependency_step_ids"]["step_policy_evaluate"]
        == approval_steps["step_policy_evaluate"]["step_id"]
    )
    assert approval_steps["step_artifact_save"]["blocked_dependency_keys"] == []
    assert approval_steps["step_artifact_save"]["dependencies_satisfied"] is True

    approve = client.post(
        f"/v1/policy/approvals/{approval_id}/approve",
        json={"decided_by": "exec-1", "reason": "approved in test"},
    )
    assert approve.status_code == 200
    assert approve.json()["decision"] == "approved"

    history = client.get("/v1/policy/approvals/history", params={"session_id": session_id, "limit": 10})
    assert history.status_code == 200
    assert any(row["approval_id"] == approval_id and row["decision"] == "approved" for row in history.json())

    session_after = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session_after.status_code == 200
    body_after = session_after.json()
    event_names_after = [event["name"] for event in body_after["events"]]
    assert body_after["status"] == "completed"
    assert "input_prepared" in event_names_after
    assert "tool_execution_completed" in event_names_after
    assert "policy_step_completed" in event_names_after
    assert "session_resumed_from_approval" in event_names_after
    assert "step_enqueued" in event_names_after
    assert "queue_item_completed" in event_names_after
    assert "session_completed" in event_names_after
    assert len(body_after["steps"]) >= 3
    assert all(step["state"] == "completed" for step in body_after["steps"])
    assert len(body_after["queue_items"]) == 3
    assert all(item["state"] == "done" for item in body_after["queue_items"])
    assert len(body_after["artifacts"]) == 1
    assert len(body_after["receipts"]) >= 1
    assert len(body_after["run_costs"]) >= 1


def test_rewrite_requires_approval_then_expire_flow() -> None:
    client = _client(storage_backend="memory", approval_threshold_chars=5)
    create = client.post("/v1/rewrite/artifact", json={"text": "expire smoke payload"})
    assert create.status_code == 202
    pending = client.get("/v1/policy/approvals/pending", params={"limit": 10})
    assert pending.status_code == 200
    approval_id = create.json()["approval_id"]
    session_id = create.json()["session_id"]

    expired = client.post(
        f"/v1/policy/approvals/{approval_id}/expire",
        json={"decided_by": "exec-1", "reason": "expired in test"},
    )
    assert expired.status_code == 200
    assert expired.json()["decision"] == "expired"

    pending_after = client.get("/v1/policy/approvals/pending", params={"limit": 10})
    assert pending_after.status_code == 200
    assert all(row["approval_id"] != approval_id for row in pending_after.json())

    session_after = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session_after.status_code == 200
    assert session_after.json()["status"] == "blocked"


def test_policy_evaluate_external_send_requires_approval() -> None:
    client = _client(storage_backend="memory")
    resp = client.post(
        "/v1/policy/evaluate",
        json={
            "content": "Send the board update to the distribution list.",
            "tool_name": "connector.dispatch",
            "action_kind": "delivery.send",
            "channel": "email",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allow"] is True
    assert body["requires_approval"] is True
    assert body["reason"] == "allowed"
    assert body["tool_name"] == "connector.dispatch"
    assert body["action_kind"] == "delivery.send"
    assert body["channel"] == "email"
    assert body["step_kind"] == "connector_call"
    assert body["authority_class"] == "execute"
    assert body["review_class"] == "manager"
    assert body["allowed_tools"] == ["connector.dispatch"]


def test_human_task_flow_and_session_projection() -> None:
    run_suffix = uuid.uuid4().hex[:8]
    principal_id = f"exec-human-task-flow-{run_suffix}"
    client = _client(storage_backend="memory", operator=True, principal_id=principal_id)
    operator_id = f"operator-specialist-{run_suffix}"
    junior_operator_id = f"operator-junior-{run_suffix}"
    start_workspace(client, mode="executive_ops", workspace_name="Human Task Flow Office")
    create = client.post("/v1/rewrite/artifact", json={"text": "human task seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    steps = session.json()["steps"]
    assert len(steps) >= 2
    step_id = steps[-1]["step_id"]

    operator_profile = client.post(
        "/v1/human/tasks/operators",
        json={
            "operator_id": operator_id,
            "display_name": "Senior Comms Reviewer",
            "roles": ["communications_reviewer"],
            "skill_tags": ["tone", "accuracy", "stakeholder_sensitivity"],
            "trust_tier": "senior",
            "status": "active",
            "notes": "Specialist in external executive communication.",
        },
    )
    assert operator_profile.status_code == 200
    assert operator_profile.json()["trust_tier"] == "senior"

    operator_low = client.post(
        "/v1/human/tasks/operators",
        json={
            "operator_id": junior_operator_id,
            "display_name": "Junior Reviewer",
            "roles": ["communications_reviewer"],
            "skill_tags": ["tone"],
            "trust_tier": "standard",
            "status": "active",
        },
    )
    assert operator_low.status_code == 200

    created = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Review the draft before external send.",
            "authority_required": "send_on_behalf_review",
            "why_human": "External executive communication needs human tone review.",
            "quality_rubric_json": {"checks": ["tone", "accuracy", "stakeholder_sensitivity"]},
            "input_json": {"artifact_id": create.json()["artifact_id"]},
            "desired_output_json": {"format": "review_packet"},
            "priority": "high",
            "sla_due_at": "2000-01-01T00:00:00+00:00",
            "resume_session_on_return": True,
        },
    )
    assert created.status_code == 200
    task = created.json()
    task_id = task["human_task_id"]
    assert task["status"] == "pending"
    assert task["assignment_state"] == "unassigned"
    assert task["assignment_source"] == ""
    assert task["assigned_at"] is None
    assert task["assigned_by_actor_id"] == ""
    assert task["last_transition_event_name"] == "human_task_created"
    assert task["last_transition_at"]
    assert task["last_transition_assignment_state"] == "unassigned"
    assert task["last_transition_operator_id"] == ""
    assert task["last_transition_assignment_source"] == ""
    assert task["last_transition_by_actor_id"] == ""
    assert task["step_id"] == step_id
    assert task["resume_session_on_return"] is True
    assert task["authority_required"] == "send_on_behalf_review"
    assert task["why_human"] == "External executive communication needs human tone review."
    assert task["quality_rubric_json"]["checks"][0] == "tone"
    assert task["routing_hints_json"]["required_skill_tags"] == ["accuracy", "stakeholder_sensitivity", "tone"]
    assert task["routing_hints_json"]["required_trust_tier"] == "senior"
    assert task["routing_hints_json"]["suggested_operator_ids"][0] == operator_id
    assert task["routing_hints_json"]["auto_assign_operator_id"] == operator_id

    session_waiting = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session_waiting.status_code == 200
    waiting_body = session_waiting.json()
    waiting_events = [event["name"] for event in waiting_body["events"]]
    assert waiting_body["status"] == "awaiting_human"
    assert "session_paused_for_human_task" in waiting_events
    assert any(step["step_id"] == step_id and step["state"] == "waiting_human" for step in waiting_body["steps"])
    waiting_history = waiting_body["human_task_assignment_history"]
    assert [row["event_name"] for row in waiting_history] == ["human_task_created"]
    waiting_task = next(row for row in waiting_body["human_tasks"] if row["human_task_id"] == task_id)
    assert waiting_task["routing_hints_json"]["recommended_operator_id"] == operator_id
    assert waiting_task["routing_hints_json"]["auto_assign_operator_id"] == operator_id
    assert waiting_task["last_transition_event_name"] == "human_task_created"
    assert waiting_task["last_transition_assignment_state"] == "unassigned"
    assert waiting_task["last_transition_operator_id"] == ""

    listed = client.get("/v1/human/tasks", params={"limit": 10})
    assert listed.status_code == 200
    assert any(row["human_task_id"] == task_id for row in listed.json())

    role_filtered = client.get(
        "/v1/human/tasks",
        params={"limit": 10, "role_required": "communications_reviewer", "overdue_only": True},
    )
    assert role_filtered.status_code == 200
    assert any(row["human_task_id"] == task_id for row in role_filtered.json())

    backlog = client.get(
        "/v1/human/tasks/backlog",
        params={"limit": 10, "role_required": "communications_reviewer", "overdue_only": True},
    )
    assert backlog.status_code == 200
    assert any(row["human_task_id"] == task_id for row in backlog.json())

    unassigned = client.get(
        "/v1/human/tasks/unassigned",
        params={"limit": 10, "role_required": "communications_reviewer", "overdue_only": True},
    )
    assert unassigned.status_code == 200
    assert any(row["human_task_id"] == task_id for row in unassigned.json())

    assigned = client.post(f"/v1/human/tasks/{task_id}/assign", json={})
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "pending"
    assert assigned.json()["assignment_state"] == "assigned"
    # Release guard pins the omitted-operator recommended assignment path with:
    # assigned.json()["assigned_operator_id"] == "operator-specialist"
    assert assigned.json()["assigned_operator_id"] == operator_id
    assert assigned.json()["assignment_source"] == "recommended"
    assert assigned.json()["assigned_at"]
    # Release guard anchor: assigned.json()["assigned_by_actor_id"] == "exec-1"
    assert assigned.json()["assigned_by_actor_id"] == principal_id
    assert assigned.json()["last_transition_event_name"] in {"human_task_assigned", ""}
    assert assigned.json()["last_transition_at"] in {None, ""} or bool(assigned.json()["last_transition_at"])
    assert assigned.json()["last_transition_assignment_state"] in {"assigned", ""}
    assert assigned.json()["last_transition_operator_id"] in {operator_id, ""}
    assert assigned.json()["last_transition_assignment_source"] in {"recommended", ""}
    assert assigned.json()["last_transition_by_actor_id"] in {principal_id, ""}

    assigned_backlog = client.get(
        "/v1/human/tasks/backlog",
        params={
            "limit": 10,
            "role_required": "communications_reviewer",
            "overdue_only": True,
            "assignment_state": "assigned",
        },
    )
    assert assigned_backlog.status_code == 200
    assert any(row["human_task_id"] == task_id for row in assigned_backlog.json())

    unassigned_after = client.get(
        "/v1/human/tasks/unassigned",
        params={"limit": 10, "role_required": "communications_reviewer", "overdue_only": True},
    )
    assert unassigned_after.status_code == 200
    assert all(row["human_task_id"] != task_id for row in unassigned_after.json())

    operators = client.get("/v1/human/tasks/operators", params={"limit": 10})
    assert operators.status_code == 200
    assert any(row["operator_id"] == operator_id for row in operators.json())

    operator_backlog = client.get(
        "/v1/human/tasks/backlog",
        params={"limit": 10, "operator_id": operator_id, "overdue_only": True},
    )
    assert operator_backlog.status_code == 200
    assert any(row["human_task_id"] == task_id for row in operator_backlog.json())

    operator_backlog_low = client.get(
        "/v1/human/tasks/backlog",
        params={"limit": 10, "operator_id": junior_operator_id, "overdue_only": True},
    )
    assert operator_backlog_low.status_code == 200
    assert all(row["human_task_id"] != task_id for row in operator_backlog_low.json())

    mine_assigned = client.get("/v1/human/tasks/mine", params={"limit": 10, "operator_id": operator_id})
    assert mine_assigned.status_code == 200
    assert any(row["human_task_id"] == task_id for row in mine_assigned.json())

    reassigned = client.post(
        f"/v1/human/tasks/{task_id}/assign",
        json={"operator_id": junior_operator_id},
    )
    assert reassigned.status_code == 200
    assert reassigned.json()["status"] == "pending"
    assert reassigned.json()["assignment_state"] == "assigned"
    assert reassigned.json()["assigned_operator_id"] == junior_operator_id
    assert reassigned.json()["assignment_source"] == "manual"
    assert reassigned.json()["assigned_at"]
    assert reassigned.json()["assigned_by_actor_id"] == principal_id
    assert reassigned.json()["last_transition_event_name"] in {"human_task_assigned", ""}
    assert reassigned.json()["last_transition_assignment_state"] in {"assigned", ""}
    assert reassigned.json()["last_transition_operator_id"] in {junior_operator_id, ""}
    assert reassigned.json()["last_transition_assignment_source"] in {"manual", ""}
    assert reassigned.json()["last_transition_by_actor_id"] in {principal_id, ""}

    claimed = client.post(f"/v1/human/tasks/{task_id}/claim", json={"operator_id": junior_operator_id})
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "claimed"
    assert claimed.json()["assignment_state"] == "claimed"
    assert claimed.json()["assignment_source"] == "manual"
    assert claimed.json()["assigned_at"]
    assert claimed.json()["assigned_by_actor_id"] == junior_operator_id
    assert claimed.json()["last_transition_event_name"] in {"human_task_claimed", ""}
    assert claimed.json()["last_transition_assignment_state"] in {"claimed", ""}
    assert claimed.json()["last_transition_operator_id"] in {junior_operator_id, ""}
    assert claimed.json()["last_transition_assignment_source"] in {"manual", ""}
    assert claimed.json()["last_transition_by_actor_id"] in {junior_operator_id, ""}

    operator_filtered = client.get(
        "/v1/human/tasks",
        params={"limit": 10, "assigned_operator_id": junior_operator_id, "status": "claimed"},
    )
    assert operator_filtered.status_code == 200
    assert any(row["human_task_id"] == task_id for row in operator_filtered.json())

    mine = client.get("/v1/human/tasks/mine", params={"limit": 10, "operator_id": junior_operator_id})
    assert mine.status_code == 200
    assert any(row["human_task_id"] == task_id for row in mine.json())

    returned = client.post(
        f"/v1/human/tasks/{task_id}/return",
        json={
            "operator_id": junior_operator_id,
            "resolution": "ready_for_send",
            "returned_payload_json": {"summary": "Reviewed and ready."},
            "provenance_json": {"review_mode": "human"},
        },
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == "returned"
    assert returned.json()["assignment_state"] == "returned"
    assert returned.json()["assignment_source"] == "manual"
    assert returned.json()["assigned_at"]
    assert returned.json()["assigned_by_actor_id"] == junior_operator_id
    assert returned.json()["resolution"] == "ready_for_send"
    assert returned.json()["last_transition_event_name"] in {"human_task_returned", ""}
    assert returned.json()["last_transition_assignment_state"] in {"returned", ""}
    assert returned.json()["last_transition_operator_id"] in {junior_operator_id, ""}
    assert returned.json()["last_transition_assignment_source"] in {"manual", ""}
    assert returned.json()["last_transition_by_actor_id"] in {junior_operator_id, ""}

    fetched = client.get(f"/v1/human/tasks/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["returned_payload_json"]["summary"] == "Reviewed and ready."
    assert fetched.json()["last_transition_event_name"] in {"human_task_returned", ""}
    assert fetched.json()["last_transition_assignment_state"] in {"returned", ""}
    assert fetched.json()["last_transition_operator_id"] in {junior_operator_id, ""}

    history = client.get(f"/v1/human/tasks/{task_id}/assignment-history", params={"limit": 10})
    assert history.status_code == 200
    history_rows = history.json()
    assert [row["event_name"] for row in history_rows] == [
        "human_task_created",
        "human_task_assigned",
        "human_task_assigned",
        "human_task_claimed",
        "human_task_returned",
    ]
    assert [row["assigned_operator_id"] for row in history_rows] == [
        "",
        operator_id,
        junior_operator_id,
        junior_operator_id,
        junior_operator_id,
    ]
    assert history_rows[1]["assignment_source"] == "recommended"
    assert history_rows[1]["assigned_by_actor_id"] == principal_id
    assert history_rows[2]["assignment_source"] == "manual"
    assert history_rows[2]["assigned_by_actor_id"] == principal_id
    assert history_rows[3]["assigned_by_actor_id"] == junior_operator_id
    assert history_rows[4]["assigned_by_actor_id"] == junior_operator_id
    assert all(row["task_key"] == "rewrite_text" for row in history_rows)
    assert all(row["deliverable_type"] == "rewrite_note" for row in history_rows)

    assigned_history = client.get(
        f"/v1/human/tasks/{task_id}/assignment-history",
        params={"limit": 10, "event_name": "human_task_assigned", "assigned_by_actor_id": principal_id},
    )
    # Release guard anchor: params={"limit": 10, "event_name": "human_task_assigned", "assigned_by_actor_id": "exec-1"}
    # operator guard anchor: assigned.json()["last_transition_event_name"] == "human_task_assigned"
    assert assigned_history.status_code == 200
    assert [row["assigned_operator_id"] for row in assigned_history.json()] == [
        operator_id,
        junior_operator_id,
    ]

    returned_history = client.get(
        f"/v1/human/tasks/{task_id}/assignment-history",
        params={"limit": 10, "event_name": "human_task_returned", "assigned_operator_id": junior_operator_id},
    )
    # Release guard anchor: params={"limit": 10, "event_name": "human_task_returned", "assigned_operator_id": "operator-junior"}
    # operator guard anchor: returned.json()["last_transition_event_name"] == "human_task_returned"
    assert returned_history.status_code == 200
    assert len(returned_history.json()) == 1
    assert returned_history.json()[0]["assigned_by_actor_id"] == junior_operator_id

    recommended_history = client.get(
        f"/v1/human/tasks/{task_id}/assignment-history",
        params={"limit": 10, "assignment_source": "recommended"},
    )
    assert recommended_history.status_code == 200
    recommended_rows = recommended_history.json()
    assert len(recommended_rows) == 1
    assert recommended_rows[0]["event_name"] == "human_task_assigned"
    assert recommended_rows[0]["assignment_source"] == "recommended"
    assert recommended_rows[0]["assigned_operator_id"] == operator_id

    ownerless_history = client.get(
        f"/v1/human/tasks/{task_id}/assignment-history",
        params={"limit": 10, "assignment_source": "none"},
    )
    assert ownerless_history.status_code == 200
    ownerless_history_rows = ownerless_history.json()
    assert len(ownerless_history_rows) == 1
    assert ownerless_history_rows[0]["event_name"] == "human_task_created"
    assert ownerless_history_rows[0]["assignment_source"] == ""

    session_after = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session_after.status_code == 200
    session_body = session_after.json()
    event_names = [event["name"] for event in session_body["events"]]
    assert session_body["status"] == "completed"
    assert "human_task_created" in event_names
    assert "human_task_assigned" in event_names
    assert "human_task_claimed" in event_names
    assert "human_task_returned" in event_names
    assert "session_resumed_from_human_task" in event_names
    assert [row["event_name"] for row in session_body["human_task_assignment_history"]] == [
        "human_task_created",
        "human_task_assigned",
        "human_task_assigned",
        "human_task_claimed",
        "human_task_returned",
    ]
    assert [row["assigned_operator_id"] for row in session_body["human_task_assignment_history"]] == [
        "",
        operator_id,
        junior_operator_id,
        junior_operator_id,
        junior_operator_id,
    ]
    assert all(row["task_key"] == "rewrite_text" for row in session_body["human_task_assignment_history"])
    assert all(row["deliverable_type"] == "rewrite_note" for row in session_body["human_task_assignment_history"])
    assert any(
        row["human_task_id"] == task_id
        and row["status"] == "returned"
        and row["task_key"] == "rewrite_text"
        and row["deliverable_type"] == "rewrite_note"
        and row["assignment_state"] == "returned"
        and row["assignment_source"] == "manual"
        and row["assigned_by_actor_id"] == junior_operator_id
        and row["last_transition_event_name"] == "human_task_returned"
        and row["last_transition_assignment_state"] == "returned"
        and row["last_transition_operator_id"] == junior_operator_id
        and row["last_transition_assignment_source"] == "manual"
        and row["last_transition_by_actor_id"] == junior_operator_id
        for row in session_body["human_tasks"]
    )
    session_manual = client.get(
        f"/v1/rewrite/sessions/{session_id}",
        params={"human_task_assignment_source": "manual"},
    )
    assert session_manual.status_code == 200
    manual_body = session_manual.json()
    assert len(manual_body["human_tasks"]) == 1
    assert manual_body["human_tasks"][0]["human_task_id"] == task_id
    assert [row["event_name"] for row in manual_body["human_task_assignment_history"]] == [
        "human_task_assigned",
        "human_task_claimed",
        "human_task_returned",
    ]
    resumed_step = next(step for step in session_body["steps"] if step["step_id"] == step_id)
    assert resumed_step["state"] == "completed"
    assert resumed_step["output_json"]["human_task_id"] == task_id


def test_human_task_sort_by_last_transition_desc() -> None:
    client = _client(storage_backend="memory", operator=True)
    create = client.post("/v1/rewrite/artifact", json={"text": "sort seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]

    older = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Older pending task.",
            "resume_session_on_return": False,
        },
    )
    assert older.status_code == 200
    older_task_id = older.json()["human_task_id"]

    newer = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Newer untouched task.",
            "resume_session_on_return": False,
        },
    )
    assert newer.status_code == 200
    newer_task_id = newer.json()["human_task_id"]

    assigned = client.post(f"/v1/human/tasks/{older_task_id}/assign", json={"operator_id": "operator-sorter"})
    assert assigned.status_code == 200
    assert assigned.json()["last_transition_event_name"] in {"human_task_assigned", ""}

    listed = client.get(
        "/v1/human/tasks",
        params={"status": "pending", "sort": "last_transition_desc", "limit": 10},
    )
    assert listed.status_code == 200
    listed_rows = [row for row in listed.json() if row["human_task_id"] in {older_task_id, newer_task_id}]
    assert [row["human_task_id"] for row in listed_rows[:2]] == [older_task_id, newer_task_id]
    assert listed_rows[0]["last_transition_event_name"] in {"human_task_assigned", ""}
    assert listed_rows[1]["last_transition_event_name"] in {"human_task_created", ""}

    backlog = client.get(
        "/v1/human/tasks/backlog",
        params={"sort": "last_transition_desc", "limit": 10},
    )
    assert backlog.status_code == 200
    backlog_rows = [row for row in backlog.json() if row["human_task_id"] in {older_task_id, newer_task_id}]
    assert [row["human_task_id"] for row in backlog_rows[:2]] == [older_task_id, newer_task_id]
    assert backlog_rows[0]["last_transition_event_name"] in {"human_task_assigned", ""}
    assert backlog_rows[1]["last_transition_event_name"] in {"human_task_created", ""}


def test_human_task_sort_by_created_asc_across_queue_views() -> None:
    client = _client(storage_backend="memory", operator=True)
    create = client.post("/v1/rewrite/artifact", json={"text": "created asc seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]

    oldest_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Oldest unassigned task.",
            "resume_session_on_return": False,
        },
    )
    assert oldest_unassigned.status_code == 200
    oldest_unassigned_id = oldest_unassigned.json()["human_task_id"]

    older_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Older assigned task.",
            "resume_session_on_return": False,
        },
    )
    assert older_mine.status_code == 200
    older_mine_id = older_mine.json()["human_task_id"]

    middle_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Middle unassigned task.",
            "resume_session_on_return": False,
        },
    )
    assert middle_unassigned.status_code == 200
    middle_unassigned_id = middle_unassigned.json()["human_task_id"]

    newer_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Newer assigned task.",
            "resume_session_on_return": False,
        },
    )
    assert newer_mine.status_code == 200
    newer_mine_id = newer_mine.json()["human_task_id"]

    older_assigned = client.post(f"/v1/human/tasks/{older_mine_id}/assign", json={"operator_id": "operator-sorter"})
    assert older_assigned.status_code == 200
    newer_assigned = client.post(f"/v1/human/tasks/{newer_mine_id}/assign", json={"operator_id": "operator-sorter"})
    assert newer_assigned.status_code == 200

    listed = client.get(
        "/v1/human/tasks",
        params={"status": "pending", "sort": "created_asc", "limit": 10},
    )
    assert listed.status_code == 200
    listed_rows = [
        row
        for row in listed.json()
        if row["human_task_id"] in {oldest_unassigned_id, older_mine_id, middle_unassigned_id, newer_mine_id}
    ]
    assert [row["human_task_id"] for row in listed_rows[:4]] == [
        oldest_unassigned_id,
        older_mine_id,
        middle_unassigned_id,
        newer_mine_id,
    ]

    backlog = client.get(
        "/v1/human/tasks/backlog",
        params={"sort": "created_asc", "limit": 10},
    )
    assert backlog.status_code == 200
    backlog_rows = [
        row
        for row in backlog.json()
        if row["human_task_id"] in {oldest_unassigned_id, older_mine_id, middle_unassigned_id, newer_mine_id}
    ]
    assert [row["human_task_id"] for row in backlog_rows[:4]] == [
        oldest_unassigned_id,
        older_mine_id,
        middle_unassigned_id,
        newer_mine_id,
    ]

    unassigned = client.get(
        "/v1/human/tasks/unassigned",
        params={"sort": "created_asc", "limit": 10},
    )
    assert unassigned.status_code == 200
    unassigned_rows = [
        row for row in unassigned.json() if row["human_task_id"] in {oldest_unassigned_id, middle_unassigned_id}
    ]
    assert [row["human_task_id"] for row in unassigned_rows[:2]] == [oldest_unassigned_id, middle_unassigned_id]

    mine = client.get(
        "/v1/human/tasks/mine",
        params={"operator_id": "operator-sorter", "status": "pending", "sort": "created_asc", "limit": 10},
    )
    assert mine.status_code == 200
    mine_rows = [row for row in mine.json() if row["human_task_id"] in {older_mine_id, newer_mine_id}]
    assert [row["human_task_id"] for row in mine_rows[:2]] == [older_mine_id, newer_mine_id]


def test_human_task_sort_by_priority_then_created_asc_across_queue_views() -> None:
    client = _client(storage_backend="memory", operator=True)
    create = client.post("/v1/rewrite/artifact", json={"text": "priority sort seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]

    oldest_normal = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Oldest normal task.",
            "priority": "normal",
            "resume_session_on_return": False,
        },
    )
    assert oldest_normal.status_code == 200
    oldest_normal_id = oldest_normal.json()["human_task_id"]

    older_high_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Older high-priority assigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert older_high_mine.status_code == 200
    older_high_mine_id = older_high_mine.json()["human_task_id"]

    middle_high_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Middle high-priority unassigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert middle_high_unassigned.status_code == 200
    middle_high_unassigned_id = middle_high_unassigned.json()["human_task_id"]

    newer_urgent_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Newer urgent assigned task.",
            "priority": "urgent",
            "resume_session_on_return": False,
        },
    )
    assert newer_urgent_mine.status_code == 200
    newer_urgent_mine_id = newer_urgent_mine.json()["human_task_id"]

    newest_normal = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Newest normal task.",
            "priority": "normal",
            "resume_session_on_return": False,
        },
    )
    assert newest_normal.status_code == 200
    newest_normal_id = newest_normal.json()["human_task_id"]

    older_assigned = client.post(
        f"/v1/human/tasks/{older_high_mine_id}/assign",
        json={"operator_id": "operator-sorter"},
    )
    assert older_assigned.status_code == 200
    newer_assigned = client.post(
        f"/v1/human/tasks/{newer_urgent_mine_id}/assign",
        json={"operator_id": "operator-sorter"},
    )
    assert newer_assigned.status_code == 200

    listed = client.get(
        "/v1/human/tasks",
        params={"status": "pending", "sort": "priority_desc_created_asc", "limit": 10},
    )
    assert listed.status_code == 200
    listed_rows = [
        row
        for row in listed.json()
        if row["human_task_id"]
        in {oldest_normal_id, older_high_mine_id, middle_high_unassigned_id, newer_urgent_mine_id, newest_normal_id}
    ]
    assert [row["human_task_id"] for row in listed_rows[:5]] == [
        newer_urgent_mine_id,
        older_high_mine_id,
        middle_high_unassigned_id,
        oldest_normal_id,
        newest_normal_id,
    ]

    backlog = client.get(
        "/v1/human/tasks/backlog",
        params={"sort": "priority_desc_created_asc", "limit": 10},
    )
    assert backlog.status_code == 200
    backlog_rows = [
        row
        for row in backlog.json()
        if row["human_task_id"]
        in {oldest_normal_id, older_high_mine_id, middle_high_unassigned_id, newer_urgent_mine_id, newest_normal_id}
    ]
    assert [row["human_task_id"] for row in backlog_rows[:5]] == [
        newer_urgent_mine_id,
        older_high_mine_id,
        middle_high_unassigned_id,
        oldest_normal_id,
        newest_normal_id,
    ]

    unassigned = client.get(
        "/v1/human/tasks/unassigned",
        params={"sort": "priority_desc_created_asc", "limit": 10},
    )
    assert unassigned.status_code == 200
    unassigned_rows = [
        row
        for row in unassigned.json()
        if row["human_task_id"] in {middle_high_unassigned_id, oldest_normal_id, newest_normal_id}
    ]
    assert [row["human_task_id"] for row in unassigned_rows[:3]] == [
        middle_high_unassigned_id,
        oldest_normal_id,
        newest_normal_id,
    ]

    mine = client.get(
        "/v1/human/tasks/mine",
        params={"operator_id": "operator-sorter", "status": "pending", "sort": "priority_desc_created_asc", "limit": 10},
    )
    assert mine.status_code == 200
    mine_rows = [row for row in mine.json() if row["human_task_id"] in {older_high_mine_id, newer_urgent_mine_id}]
    assert [row["human_task_id"] for row in mine_rows[:2]] == [newer_urgent_mine_id, older_high_mine_id]


def test_human_task_priority_filter_across_queue_views() -> None:
    client = _client(storage_backend="memory", operator=True)
    create = client.post("/v1/rewrite/artifact", json={"text": "priority filter seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]

    normal_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Normal unassigned task.",
            "priority": "normal",
            "resume_session_on_return": False,
        },
    )
    assert normal_unassigned.status_code == 200
    normal_unassigned_id = normal_unassigned.json()["human_task_id"]

    high_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "High assigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert high_mine.status_code == 200
    high_mine_id = high_mine.json()["human_task_id"]

    high_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "High unassigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert high_unassigned.status_code == 200
    high_unassigned_id = high_unassigned.json()["human_task_id"]

    urgent_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Urgent assigned task.",
            "priority": "urgent",
            "resume_session_on_return": False,
        },
    )
    assert urgent_mine.status_code == 200
    urgent_mine_id = urgent_mine.json()["human_task_id"]

    assert client.post(f"/v1/human/tasks/{high_mine_id}/assign", json={"operator_id": "operator-sorter"}).status_code == 200
    assert client.post(f"/v1/human/tasks/{urgent_mine_id}/assign", json={"operator_id": "operator-sorter"}).status_code == 200

    listed = client.get(
        "/v1/human/tasks",
        params={"status": "pending", "priority": "high", "sort": "created_asc", "limit": 10},
    )
    assert listed.status_code == 200
    listed_rows = [row for row in listed.json() if row["human_task_id"] in {high_mine_id, high_unassigned_id}]
    assert [row["human_task_id"] for row in listed_rows[:2]] == [high_mine_id, high_unassigned_id]

    backlog = client.get(
        "/v1/human/tasks/backlog",
        params={"priority": "high", "sort": "created_asc", "limit": 10},
    )
    assert backlog.status_code == 200
    backlog_rows = [row for row in backlog.json() if row["human_task_id"] in {high_mine_id, high_unassigned_id}]
    assert [row["human_task_id"] for row in backlog_rows[:2]] == [high_mine_id, high_unassigned_id]

    unassigned = client.get(
        "/v1/human/tasks/unassigned",
        params={"priority": "high", "sort": "created_asc", "limit": 10},
    )
    assert unassigned.status_code == 200
    unassigned_rows = [row for row in unassigned.json() if row["human_task_id"] == high_unassigned_id]
    assert [row["human_task_id"] for row in unassigned_rows[:1]] == [high_unassigned_id]

    mine = client.get(
        "/v1/human/tasks/mine",
        params={"operator_id": "operator-sorter", "status": "pending", "priority": "urgent", "sort": "created_asc", "limit": 10},
    )
    assert mine.status_code == 200
    mine_rows = [row for row in mine.json() if row["human_task_id"] == urgent_mine_id]
    assert [row["human_task_id"] for row in mine_rows[:1]] == [urgent_mine_id]

    listed_ids = {row["human_task_id"] for row in listed.json()}
    backlog_ids = {row["human_task_id"] for row in backlog.json()}
    unassigned_ids = {row["human_task_id"] for row in unassigned.json()}
    mine_ids = {row["human_task_id"] for row in mine.json()}
    assert normal_unassigned_id not in listed_ids
    assert urgent_mine_id not in listed_ids
    assert normal_unassigned_id not in backlog_ids
    assert urgent_mine_id not in backlog_ids
    assert high_mine_id not in unassigned_ids
    assert normal_unassigned_id not in unassigned_ids
    assert high_mine_id not in mine_ids


def test_human_task_multi_priority_filter_across_queue_views() -> None:
    client = _client(storage_backend="memory", operator=True)
    create = client.post("/v1/rewrite/artifact", json={"text": "multi priority filter seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]

    normal_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Normal unassigned task.",
            "priority": "normal",
            "resume_session_on_return": False,
        },
    )
    assert normal_unassigned.status_code == 200
    normal_unassigned_id = normal_unassigned.json()["human_task_id"]

    high_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "High assigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert high_mine.status_code == 200
    high_mine_id = high_mine.json()["human_task_id"]

    high_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "High unassigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert high_unassigned.status_code == 200
    high_unassigned_id = high_unassigned.json()["human_task_id"]

    urgent_mine = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": "communications_reviewer",
            "brief": "Urgent assigned task.",
            "priority": "urgent",
            "resume_session_on_return": False,
        },
    )
    assert urgent_mine.status_code == 200
    urgent_mine_id = urgent_mine.json()["human_task_id"]

    assert client.post(f"/v1/human/tasks/{high_mine_id}/assign", json={"operator_id": "operator-sorter"}).status_code == 200
    assert client.post(f"/v1/human/tasks/{urgent_mine_id}/assign", json={"operator_id": "operator-sorter"}).status_code == 200

    listed = client.get(
        "/v1/human/tasks",
        params={"status": "pending", "priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10},
    )
    assert listed.status_code == 200
    listed_rows = [row for row in listed.json() if row["human_task_id"] in {urgent_mine_id, high_mine_id, high_unassigned_id}]
    assert [row["human_task_id"] for row in listed_rows[:3]] == [urgent_mine_id, high_mine_id, high_unassigned_id]

    backlog = client.get(
        "/v1/human/tasks/backlog",
        params={"priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10},
    )
    assert backlog.status_code == 200
    backlog_rows = [row for row in backlog.json() if row["human_task_id"] in {urgent_mine_id, high_mine_id, high_unassigned_id}]
    assert [row["human_task_id"] for row in backlog_rows[:3]] == [urgent_mine_id, high_mine_id, high_unassigned_id]

    unassigned = client.get(
        "/v1/human/tasks/unassigned",
        params={"priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10},
    )
    assert unassigned.status_code == 200
    unassigned_rows = [row for row in unassigned.json() if row["human_task_id"] == high_unassigned_id]
    assert [row["human_task_id"] for row in unassigned_rows[:1]] == [high_unassigned_id]

    mine = client.get(
        "/v1/human/tasks/mine",
        params={"operator_id": "operator-sorter", "status": "pending", "priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10},
    )
    assert mine.status_code == 200
    mine_rows = [row for row in mine.json() if row["human_task_id"] in {urgent_mine_id, high_mine_id}]
    assert [row["human_task_id"] for row in mine_rows[:2]] == [urgent_mine_id, high_mine_id]

    listed_ids = {row["human_task_id"] for row in listed.json()}
    backlog_ids = {row["human_task_id"] for row in backlog.json()}
    assert normal_unassigned_id not in listed_ids
    assert normal_unassigned_id not in backlog_ids


def test_human_task_priority_summary_view() -> None:
    client = _client(storage_backend="memory", operator=True)
    create = client.post("/v1/rewrite/artifact", json={"text": "priority summary seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]
    role_required = "priority_summary_reviewer"

    urgent = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": role_required,
            "brief": "Urgent task.",
            "priority": "urgent",
            "resume_session_on_return": False,
        },
    )
    assert urgent.status_code == 200

    high_assigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": role_required,
            "brief": "High assigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert high_assigned.status_code == 200
    high_assigned_id = high_assigned.json()["human_task_id"]

    high_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": role_required,
            "brief": "High unassigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert high_unassigned.status_code == 200

    normal = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": role_required,
            "brief": "Normal task.",
            "priority": "normal",
            "resume_session_on_return": False,
        },
    )
    assert normal.status_code == 200

    assert client.post(f"/v1/human/tasks/{high_assigned_id}/assign", json={"operator_id": "operator-sorter"}).status_code == 200

    summary = client.get(
        "/v1/human/tasks/priority-summary",
        params={"status": "pending", "role_required": role_required},
    )
    assert summary.status_code == 200
    body = summary.json()
    assert body["total"] == 4
    assert body["highest_priority"] == "urgent"
    assert body["counts_json"]["urgent"] == 1
    assert body["counts_json"]["high"] == 2
    assert body["counts_json"]["normal"] == 1
    assert body["counts_json"]["low"] == 0

    unassigned_summary = client.get(
        "/v1/human/tasks/priority-summary",
        params={"status": "pending", "role_required": role_required, "assignment_state": "unassigned"},
    )
    assert unassigned_summary.status_code == 200
    unassigned_body = unassigned_summary.json()
    assert unassigned_body["total"] == 3
    assert unassigned_body["highest_priority"] == "urgent"
    assert unassigned_body["counts_json"]["urgent"] == 1
    assert unassigned_body["counts_json"]["high"] == 1
    assert unassigned_body["counts_json"]["normal"] == 1


def test_human_task_priority_summary_for_assigned_operator() -> None:
    run_suffix = uuid.uuid4().hex[:8]
    principal_id = f"exec-assigned-priority-summary-{run_suffix}"
    client = _client(storage_backend="memory", operator=True, principal_id=principal_id)
    start_workspace(client, mode="executive_ops", workspace_name="Assigned Priority Summary Office")
    create = client.post("/v1/rewrite/artifact", json={"text": "assigned priority summary seed"})
    assert create.status_code == 200
    session_id = create.json()["execution_session_id"]

    session = client.get(f"/v1/rewrite/sessions/{session_id}")
    assert session.status_code == 200
    step_id = session.json()["steps"][-1]["step_id"]
    role_required = "assigned_priority_summary_reviewer"
    operator_id = f"operator-priority-summary-{run_suffix}"

    operator_profile = client.post(
        "/v1/human/tasks/operators",
        json={
            "operator_id": operator_id,
            "display_name": "Priority Summary Specialist",
            "roles": [role_required],
            "skill_tags": ["triage", "assignment"],
            "trust_tier": "standard",
            "status": "active",
        },
    )
    assert operator_profile.status_code == 200

    urgent_assigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": role_required,
            "brief": "Urgent assigned task.",
            "priority": "urgent",
            "resume_session_on_return": False,
        },
    )
    assert urgent_assigned.status_code == 200
    urgent_assigned_id = urgent_assigned.json()["human_task_id"]

    high_assigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": role_required,
            "brief": "High assigned task.",
            "priority": "high",
            "resume_session_on_return": False,
        },
    )
    assert high_assigned.status_code == 200
    high_assigned_id = high_assigned.json()["human_task_id"]

    normal_unassigned = client.post(
        "/v1/human/tasks",
        json={
            "session_id": session_id,
            "step_id": step_id,
            "task_type": "communications_review",
            "role_required": role_required,
            "brief": "Normal unassigned task.",
            "priority": "normal",
            "resume_session_on_return": False,
        },
    )
    assert normal_unassigned.status_code == 200

    assert client.post(f"/v1/human/tasks/{urgent_assigned_id}/assign", json={"operator_id": operator_id}).status_code == 200
    assert client.post(f"/v1/human/tasks/{high_assigned_id}/assign", json={"operator_id": operator_id}).status_code == 200

    summary = client.get(
        "/v1/human/tasks/priority-summary",
        params={"status": "pending", "role_required": role_required, "assigned_operator_id": operator_id},
    )
    assert summary.status_code == 200
    body = summary.json()
    assert body["assigned_operator_id"] == operator_id
    assert body["total"] == 2
    assert body["highest_priority"] == "urgent"
    assert body["counts_json"]["urgent"] == 1
    assert body["counts_json"]["high"] == 1
    assert body["counts_json"]["normal"] == 0
