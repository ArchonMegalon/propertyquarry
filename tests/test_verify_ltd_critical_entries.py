from __future__ import annotations

import pytest

from scripts import verify_ltd_critical_entries as verifier


def _markdown(
    *,
    browseract_account: str = "[redacted]",
    browseract_status: str = "complete",
    browseract_source: str = "browseract_live",
    teable_account: str = "rotated-account",
    teable_status: str = "complete",
    teable_source: str = "browseract_live",
) -> str:
    return f"""
| `Prompt Architects` | `Tier 4` |
PROMPTING_SYSTEMS_API_KEY
Prompt Foundry
| `1min.AI` | `Advanced Business Plan` |
scripts/resolve_onemin_ai_key.sh
remaining credits

## Discovery Tracking
| Service | Account | Discovery Status | Verification Source | Last Verified | Notes |
|---|---|---|---|---|---|
| `BrowserAct` | {browseract_account} | `{browseract_status}` | `{browseract_source}` | now | verified |
| `Teable` | {teable_account} | `{teable_status}` | `{teable_source}` | now | verified |
"""


def _env(*, prompt_architects: str = "present") -> dict[str, str]:
    return {
        "PROMPTING_SYSTEMS_API_KEY": prompt_architects,
        "ONEMIN_AI_API_KEY": "present",
    }


def test_discovery_checks_ignore_changed_or_redacted_account_cells() -> None:
    receipt = verifier.build_receipt(
        markdown_text=_markdown(
            browseract_account="[redacted-by-operator]",
            teable_account="account-rotated-without-validator-change",
        ),
        env=_env(),
    )

    assert receipt["status"] == "pass"
    assert receipt["checks"]["browseract_discovery"] is True
    assert receipt["checks"]["teable_discovery"] is True


@pytest.mark.parametrize(
    ("service", "status", "source"),
    [
        ("BrowserAct", "pending", "browseract_live"),
        ("BrowserAct", "complete", "manual_seeded"),
        ("Teable", "pending", "browseract_live"),
        ("Teable", "complete", "manual_seeded"),
    ],
)
def test_discovery_checks_reject_wrong_status_or_source(
    service: str,
    status: str,
    source: str,
) -> None:
    overrides = {
        f"{service.lower()}_status": status,
        f"{service.lower()}_source": source,
    }
    receipt = verifier.build_receipt(
        markdown_text=_markdown(**overrides),
        env=_env(),
    )

    check_name = f"{service.lower()}_discovery"
    assert receipt["status"] == "fail"
    assert receipt["checks"][check_name] is False
    assert check_name in receipt["failures"]


def test_prompt_architects_environment_remains_fail_closed() -> None:
    receipt = verifier.build_receipt(
        markdown_text=_markdown(),
        env=_env(prompt_architects=" \t"),
    )

    assert receipt["status"] == "fail"
    assert receipt["checks"]["browseract_discovery"] is True
    assert receipt["checks"]["teable_discovery"] is True
    assert receipt["checks"]["prompt_architects_env"] is False
    assert receipt["failures"] == ["prompt_architects_env"]
