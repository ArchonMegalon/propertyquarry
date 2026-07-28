from __future__ import annotations

from scripts.verify_ltd_critical_entries import build_receipt


def test_ltd_critical_entries_gate_passes_for_expected_inventory_and_env() -> None:
    markdown = """
| `1min.AI` | `Advanced Business Plan` | `12 licenses / 12 accounts` | `Owned` |  | `Tier 1` | Local `.env` key rotation slots plus `scripts/resolve_onemin_ai_key.sh` | Latest credit refresh confirmed remaining credits. |
| `Prompt Architects` | `Tier 4` | `1 account` | `Activated` |  | `Tier 4` | `PROMPTING_SYSTEMS_API_KEY` in local `.env`; governed Prompt Foundry Accelerator is integrated for template seed/operator assist | AppSumo Tier 4 capture is confirmed. |

## Discovery Tracking

| `BrowserAct` | ops@example.com | `complete` | `browseract_live` | 2026-03-07T00:00:00Z | Plan/Tier: Tier 3; Status: activated |
| `Teable` | ops@teable.example | `complete` | `browseract_live` | 2026-03-07T00:01:00Z | Plan/Tier: License Tier 4; Status: activated |
""".strip()

    receipt = build_receipt(
        markdown_text=markdown,
        env={
            "PROMPTING_SYSTEMS_API_KEY": "pa_live_test",
            "ONEMIN_AI_API_KEY": "onemin_live_test",
        },
    )

    assert receipt["status"] == "pass"
    assert receipt["failures"] == []


def test_ltd_critical_entries_gate_fails_closed_on_missing_inventory_or_env() -> None:
    receipt = build_receipt(
        markdown_text="| `Prompt Architects` | `Tier 4` |",
        env={},
    )

    assert receipt["status"] == "fail"
    assert "prompt_architects_inventory" in receipt["failures"]
    assert "onemin_inventory" in receipt["failures"]
    assert "browseract_discovery" in receipt["failures"]
    assert "teable_discovery" in receipt["failures"]
    assert "prompt_architects_env" in receipt["failures"]
    assert "onemin_env" in receipt["failures"]
