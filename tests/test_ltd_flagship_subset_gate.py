from __future__ import annotations

from scripts.verify_ltd_flagship_subset import build_receipt


def test_ltd_flagship_subset_gate_passes_for_expected_verified_subset() -> None:
    markdown = """
## Discovery Tracking

| Service | Account / Email | Discovery Status | Verification Source | Last Verified | Notes |
|---|---|---|---|---|---|
| `1min.AI` |  | `live_provider_call_verified` | `worker_health_probe + principal_bound_provider_receipt` | 2026-08-12T20:14:50Z | ok |
| `Prompt Architects` |  | `manual_seeded` | `local_env + prompt_foundry_receipts` | 2026-06-01T20:54:48Z | ok |
| `PayFunnels` |  | `unconfigured_external_authority` | `deployed_runtime_probe + contract_tests` | 2026-08-12T21:51:56Z | ok |
| `BrowserAct` | ops@example.com | `complete` | `browseract_live` | 2026-03-07T00:00:00Z | ok |
| `Teable` | ops@teable.example | `complete` | `browseract_live` | 2026-03-07T00:01:00Z | ok |
| `ClickRank.ai` | ops@example.com | `complete` | `clickrank_live` | 2026-05-04T07:44:00Z | ok |
| `Emailit` |  | `manual_seeded` | `emailit_api_live` | 2026-05-01T05:00:00Z | ok |
| `Pixefy` | ops@example.com | `manual_seeded` | `fleet_verified` | 2026-05-29T20:16:00Z | ok |
| `Rafter` | ops@example.com | `manual_seeded` | `fleet_verified` | 2026-05-29T20:16:00Z | ok |
""".strip()

    receipt = build_receipt(markdown_text=markdown)

    assert receipt["status"] == "pass"
    assert receipt["accepted_total"] == 9
    assert receipt["live_evidence_verified_total"] == 5
    assert receipt["propertyquarry_customer_integration_verified_total"] == 1
    assert receipt["live_verified_total"] == 1
    assert receipt["contract_or_unconfigured_total"] == 4
    assert receipt["not_live_integration_gate"] is True
    assert receipt["services"]["PayFunnels"]["accepted"] is True
    assert (
        receipt["services"]["PayFunnels"]["live_integration_verified"]
        is False
    )
    assert (
        receipt["services"]["1min.AI"]["live_integration_verified"]
        is True
    )
    assert receipt["services"]["BrowserAct"]["live_evidence_verified"] is True
    assert (
        receipt["services"]["BrowserAct"][
            "propertyquarry_customer_integration_verified"
        ]
        is False
    )
    assert (
        receipt["services"]["BrowserAct"]["live_integration_verified"]
        is False
    )
    assert receipt["services"]["Emailit"]["live_evidence_verified"] is True
    assert (
        receipt["services"]["Emailit"][
            "propertyquarry_customer_integration_verified"
        ]
        is False
    )
    assert (
        receipt["services"]["1min.AI"][
            "propertyquarry_customer_integration_verified"
        ]
        is True
    )
    assert receipt["failures"] == []


def test_ltd_flagship_subset_gate_fails_closed_on_missing_or_wrong_sources() -> None:
    markdown = """
## Discovery Tracking

| Service | Account / Email | Discovery Status | Verification Source | Last Verified | Notes |
|---|---|---|---|---|---|
| `BrowserAct` | ops@example.com | `complete` | `browseract_live` | 2026-03-07T00:00:00Z | ok |
| `Teable` | ops@teable.example | `missing` | `manual_inventory` |  | wrong |
""".strip()

    receipt = build_receipt(markdown_text=markdown)

    assert receipt["status"] == "fail"
    assert "flagship_subset_mismatch:Teable:missing:manual_inventory" in receipt["failures"]
    assert "flagship_subset_mismatch:Prompt Architects:missing:missing" in receipt["failures"]
    assert "flagship_subset_coverage_below_floor:1<9" in receipt["failures"]
