from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.product.privacy_lifecycle import privacy_export_has_secret_markers, redact_privacy_export
from app.product.privacy_lifecycle_storage import clear_privacy_lifecycle_memory_for_tests
from app.product.service import build_product_service
from app.services.property_content_job_ledger import PropertyContentJobLedger
from tests.product_test_helpers import build_property_client, start_workspace


@pytest.fixture(autouse=True)
def _privacy_test_state(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_privacy_lifecycle_memory_for_tests()
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_SIGNING_SECRET", "privacy-tests-signing-secret")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVACY_EXPORT_SECRET", "privacy-tests-export-secret")


def _started_client(principal_id: str):
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name=f"Privacy {principal_id}")
    return client


def _seed_private_content(
    *,
    principal_id: str,
    packet_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PropertyContentJobLedger, Path]:
    ledger_path = tmp_path / "content-ledger.json"
    completion_dir = tmp_path / "content-receipts"
    monkeypatch.setenv("PROPERTYQUARRY_CONTENT_JOB_LEDGER", str(ledger_path))
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_COMPLETION_DIR", str(completion_dir))
    ledger = PropertyContentJobLedger(path=ledger_path)
    ownership = {
        "principal_id": principal_id,
        "ownership_scope": "search_run",
        "search_run_id": f"run-{packet_id}",
    }
    packet = {
        "packet_id": packet_id,
        "content_mode": "PROPERTY_DOSSIER",
        "source_url": "https://listing.example/private?token=content-source-secret",
        "access_token": "content-packet-secret",
    }
    receipt_path = ledger.write_receipt(
        packet=packet,
        receipt={
            "status": "review_required",
            "provider": "subscribr",
            "client_secret": "content-receipt-secret",
        },
        **ownership,
        status="HUMAN_REVIEW_REQUIRED",
    )
    payload = {
        "id": f"event-{packet_id}",
        "type": "script.generated",
        "packet_id": packet_id,
        "raw_provider_token": "content-webhook-secret",
    }
    claim = ledger.claim_webhook_event(
        event_id=str(payload["id"]),
        payload=payload,
        packet_id=packet_id,
        **ownership,
        extra={"signature_status": "verified"},
        claim_owner="privacy-test-worker",
        lease_seconds=60,
    )
    assert claim["claimed"] is True
    ledger.complete_webhook_event(
        event_id=str(payload["id"]),
        **ownership,
        claim_owner="privacy-test-worker",
        status="review_required",
    )
    return ledger, receipt_path


def test_dsar_export_is_cursor_paginated_complete_and_secret_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "pq-privacy-export"
    _ledger, receipt_path = _seed_private_content(
        principal_id=principal_id,
        packet_id="privacy-export-packet",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    client = _started_client(principal_id)
    container = client.app.state.container
    for index in range(5):
        container.channel_runtime.ingest_observation(
            principal_id=principal_id,
            channel="telegram",
            event_type="privacy_export_fixture",
            payload={
                "index": index,
                "token": "telegram-secret-token",
                "safe": f"kept-{index}",
                "private_url": f"https://provider.example/item/{index}?token=secret-{index}&view=owner",
                "signed_path": "/workspace-access/eyJhbGciOiJIUzI1NiJ9.private.signature",
            },
            source_id=f"fixture-{index}",
        )

    cursor = ""
    record_ids: list[str] = []
    pages: list[dict[str, object]] = []
    for _ in range(100):
        response = client.get(
            "/app/api/property/account/export",
            params={"limit": 2, **({"cursor": cursor} if cursor else {})},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        pages.append(page)
        record_ids.extend(str(item["record_id"]) for item in page["items"])
        cursor = str(page["pagination"]["next_cursor"] or "")
        if not cursor:
            break
    else:  # pragma: no cover - loop guard
        raise AssertionError("DSAR cursor did not terminate")

    assert pages[-1]["pagination"]["complete"] is True
    assert len(record_ids) == len(set(record_ids))
    assert len(record_ids) == int(pages[0]["pagination"]["total_records"])
    assert pages[0]["collections"]["events"] >= 5
    assert pages[0]["collections"]["property_content_studio"] >= 4
    assert "tours_and_private_receipts" in pages[0]["collections"]
    encoded = json.dumps(pages, sort_keys=True)
    assert "telegram-secret-token" not in encoded
    assert "secret-0" not in encoded
    assert "/workspace-access/eyJ" not in encoded
    assert "[REDACTED]" in encoded
    assert "kept-0" in encoded
    assert "privacy-export-packet" in encoded
    assert receipt_path.name in encoded
    assert "content-packet-secret" not in encoded
    assert "content-source-secret" not in encoded
    assert "content-receipt-secret" not in encoded
    assert "content-webhook-secret" not in encoded
    content_items = [
        item
        for page in pages
        for item in page["items"]
        if item["collection"] == "property_content_studio"
    ]
    assert {item["data"]["record_type"] for item in content_items} == {
        "job",
        "job_event",
        "webhook_event",
    }
    webhook_item = next(
        item for item in content_items if item["data"]["record_type"] == "webhook_event"
    )
    assert "payload_json" not in webhook_item["data"]
    assert not privacy_export_has_secret_markers(pages)

    wrong_tenant = _started_client("pq-privacy-export-other")
    wrong = wrong_tenant.get(
        "/app/api/property/account/export",
        params={"limit": 2, "cursor": str(pages[0]["pagination"]["next_cursor"])},
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "privacy_export_cursor_wrong_account"


def test_dsar_download_reports_complete_and_account_ui_links_it() -> None:
    client = _started_client("pq-privacy-download")

    export = client.get("/app/api/property/account/export", params={"download": 1})
    assert export.status_code == 200
    payload = export.json()
    assert payload["export_type"] == "propertyquarry_account_data"
    assert payload["export_version"] == "2.0"
    assert payload["pagination"]["complete"] is True
    assert payload["redaction_contract"]["private_tour_receipts_included_for_owner"] is True
    assert "attachment;" in export.headers["content-disposition"]
    assert export.headers["cache-control"] == "no-store"

    account = client.get("/app/account")
    assert account.status_code == 200
    assert "Download export" in account.text
    assert "private receipts" in account.text
    assert 'href="/data-deletion"' in account.text
    deletion = client.get("/data-deletion")
    assert deletion.status_code == 200
    assert "Download complete export" in deletion.text
    assert "Start deletion request" in deletion.text
    assert "Type DELETE" in deletion.text
    assert "Retry pending work" in deletion.text
    assert "digest-only erasure tombstone" in deletion.text


def test_erasure_request_is_idempotent_owner_scoped_and_cancelable() -> None:
    owner = _started_client("pq-privacy-owner")
    first = owner.post(
        "/app/api/property/account/erasure-requests",
        json={},
        headers={"Idempotency-Key": "privacy-request-1"},
    )
    second = owner.post(
        "/app/api/property/account/erasure-requests",
        json={},
        headers={"Idempotency-Key": "privacy-request-1"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    first_request = first.json()["request"]
    assert second.json()["request"]["request_id"] == first_request["request_id"]
    assert first_request["status"] == "awaiting_confirmation"
    assert first_request["can_cancel"] is True
    assert "principal_key" not in first.text
    assert "idempotency_key" not in first.text

    other = _started_client("pq-privacy-other")
    denied = other.get(f"/app/api/property/account/erasure-requests/{first_request['request_id']}")
    assert denied.status_code == 404

    cancelled = owner.post(f"/app/api/property/account/erasure-requests/{first_request['request_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["request"]["status"] == "cancelled"
    assert cancelled.json()["request"]["recovery_state"] == "closed"
    confirm_cancelled = owner.post(
        f"/app/api/property/account/erasure-requests/{first_request['request_id']}/confirm",
        json={"confirmation_phrase": "DELETE"},
        headers={"X-PropertyQuarry-Deletion-Intent": "confirm-account-erasure"},
    )
    assert confirm_cancelled.status_code == 409


def test_erasure_confirmation_revokes_sessions_tours_and_queues_provider_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "pq-privacy-confirm"
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    content_ledger, content_receipt_path = _seed_private_content(
        principal_id=principal_id,
        packet_id="privacy-erasure-packet",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    assert content_receipt_path.exists()
    client = _started_client(principal_id)
    container = client.app.state.container
    service = build_product_service(container)
    session = service.issue_workspace_access_session(
        principal_id=principal_id,
        email="owner@example.com",
        role="principal",
    )
    binding = container.provider_registry.upsert_binding_record(
        principal_id=principal_id,
        provider_key="google",
        auth_metadata_json={
            "account_email": "owner@example.com",
            "refresh_token": "must-never-export",
            "token_status": "active",
        },
    )
    slug = "privacy-owned-tour"
    bundle = tmp_path / slug
    bundle.mkdir()
    (bundle / "tour.json").write_text(
        json.dumps({"slug": slug, "title": "Owner tour", "tour_privacy_mode": "anonymous_public", "scenes": []}),
        encoding="utf-8",
    )
    (bundle / "tour.private.json").write_text(
        json.dumps(
            {
                "principal_id": principal_id,
                "recipient_email": "owner@example.com",
                "source_virtual_tour_url": "https://tour.example/private?token=secret",
            }
        ),
        encoding="utf-8",
    )

    requested = client.post(
        "/app/api/property/account/erasure-requests",
        json={"idempotency_key": "confirm-1"},
    )
    request_id = requested.json()["request"]["request_id"]
    missing_intent = client.post(
        f"/app/api/property/account/erasure-requests/{request_id}/confirm",
        json={"confirmation_phrase": "DELETE"},
    )
    assert missing_intent.status_code == 400
    wrong_phrase = client.post(
        f"/app/api/property/account/erasure-requests/{request_id}/confirm",
        json={"confirmation_phrase": "delete"},
        headers={"X-PropertyQuarry-Deletion-Intent": "confirm-account-erasure"},
    )
    assert wrong_phrase.status_code == 409

    confirmed = client.post(
        f"/app/api/property/account/erasure-requests/{request_id}/confirm",
        json={"confirmation_phrase": "DELETE"},
        headers={"X-PropertyQuarry-Deletion-Intent": "confirm-account-erasure"},
    )
    assert confirmed.status_code == 200, confirmed.text
    lifecycle = confirmed.json()["request"]
    assert lifecycle["status"] == "completed_with_provider_followup"
    assert lifecycle["recovery_state"] == "retry_available"
    assert lifecycle["provider_deletion_receipts"][0]["provider_invoked"] is False
    assert lifecycle["provider_deletion_receipts"][0]["local_binding_deleted"] is True
    content_receipt = lifecycle["local_deletion_receipts"]["property_content_studio"]
    assert content_receipt["jobs_deleted"] == 1
    assert content_receipt["job_events_deleted"] >= 3
    assert content_receipt["webhook_events_deleted"] == 1
    assert content_receipt["receipt_files_deleted"] == 1
    assert not content_receipt_path.exists()
    assert content_ledger.export_principal_data(principal_id=principal_id)["jobs"] == []
    assert container.provider_registry.get_persisted_binding_record(
        binding_id=binding.binding_id,
        principal_id=principal_id,
    ) is None
    assert service.get_workspace_access_session(
        principal_id=principal_id,
        session_id=str(session["session_id"]),
    )["status"] == "revoked"
    assert not bundle.exists()
    assert (tmp_path / ".revocations" / f"{slug}.json").exists()
    purge = json.loads((tmp_path / ".cdn-purge-outbox" / f"{slug}.json").read_text(encoding="utf-8"))
    assert purge["status"] == "queued"
    assert purge["provider_invoked"] is False

    revoked_page = client.get(f"/tours/{slug}")
    assert revoked_page.status_code == 410
    assert "removed by its owner" in revoked_page.text
    assert revoked_page.headers["cache-control"] == "no-store"
    assert revoked_page.headers["surrogate-control"] == "no-store"

    retried = client.post(f"/app/api/property/account/erasure-requests/{request_id}/retry-providers")
    assert retried.status_code == 200
    provider_receipt = retried.json()["request"]["provider_deletion_receipts"][0]
    assert provider_receipt["attempt_count"] == 1
    assert provider_receipt["provider_invoked"] is False
    assert provider_receipt["status"] == "queued_for_provider_deletion"
    assert "must-never-export" not in retried.text


def test_redaction_contract_scrubs_nested_credentials_and_signed_urls() -> None:
    payload = {
        "access_token": "eyJ.private.signature",
        "nested": {
            "client_secret": "top-secret",
            "status": "active",
            "url": "https://provider.example/callback?code=oauth-code&safe=yes#private",
            "message": "Open https://propertyquarry.com/workspace-invites/signed-token now",
        },
    }
    rendered = redact_privacy_export(payload)
    encoded = json.dumps(rendered, sort_keys=True)
    assert "top-secret" not in encoded
    assert "oauth-code" not in encoded
    assert "signed-token" not in encoded
    assert "safe=yes" in encoded
    assert "active" in encoded
