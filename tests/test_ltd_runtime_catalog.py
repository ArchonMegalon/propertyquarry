from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.api.routes.ltd_runtime import _browseract_action_ready_for_principal
from app.domain.models import ConnectorBinding
from app.services.browseract_binding_readiness import browseract_binding_supports_service
from app.services.browseract_ui_service_catalog import browseract_ui_service_by_alias
from app.services.ltd_runtime_catalog import LtdRuntimeCatalogService, _inventory_markdown_path
from app.services.provider_registry import ProviderRegistryService


def _sample_ltd_markdown() -> str:
    return """
# LTDs

Updated: 2026-05-02

## Non-AppSumo / Other LTDs

| Service | Plan / Tier | Holding | Status | Redeem By | Workspace Integration Tier | Local Integration | Notes |
|---|---|---|---|---|---|---|---|
| `1min.AI` | `Advanced Business Plan` | `12 licenses` | `Owned` |  | `Tier 1` | Local `.env` key rotation slots | Primary API-key lane is already wired. |
| `Emailit` | `Tier 5` | `1 key` | `Owned` |  | `Tier 1` | Local `.env` key plus sender-domain wiring | Transactional delivery already runs through EA. |
| `hedy.ai` | `LTD account` | `1 account` | `Owned` |  | `Tier 4` | Local `.env` username/password only | Credentials captured locally; no active runtime lane yet. |

## AppSumo LTDs

| Service | Plan / Tier | Holding | Status | Redeem By | Workspace Integration Tier | Local Integration | Notes |
|---|---|---|---|---|---|---|---|
| `Documentation.AI` | `License Tier 3` | `1 license` | `Activated` |  | `Tier 4` | Local `.env` username/password only | Owned for operator docs and cited answers. |
| `FlipLink.me` | `Tier 10` | `1 account` | `Owned` |  | `Tier 2` | Local `.env` credentials plus bounded PropertyQuarry review-packet flipbook lane | Use only for shareable redacted review packets downstream of PropertyQuarry. |
| `MarkupGo` | `7x code-based` | `7 codes` | `Activated` |  | `Tier 3` | None | BrowserAct workspace reader exists even though the direct provider lane is not executable. |
| `Poppy AI` | `Tier 6` | `1 account / 5 seats` | `Owned` |  | `Tier 3` | BrowserAct workspace-reader candidate plus local API-key placeholders only | Candidate research-board and content-intelligence lane after provider verification. |
| `Crezlo Tours` | `Tier 1` | `1 account` | `Owned` |  | `Tier 2` | BrowserAct candidate | Candidate property-tour lane. |
| `Chummer Only` | `Tier 1` | `1 account` | `Owned` |  | `Excluded - Chummer/Fleet only` | Chummer | Not a PropertyQuarry runtime integration. |

## Discovery Tracking

| Service | Account / Email | Discovery Status | Verification Source | Last Verified | Notes |
|---|---|---|---|---|---|
| `1min.AI` |  | `live_provider_call_verified` | `worker_health_probe + principal_bound_provider_receipt` | 2026-08-12T20:14:50Z | Real provider call. |
| `Emailit` |  | `manual_seeded` | `emailit_api_live` | 2026-05-01T05:00:00Z | Live sender-domain delivery. |
""".strip()


def _catalog(tmp_path: Path) -> LtdRuntimeCatalogService:
    markdown_path = tmp_path / "LTDs.md"
    markdown_path.write_text(_sample_ltd_markdown(), encoding="utf-8")
    return LtdRuntimeCatalogService(
        provider_registry=ProviderRegistryService(),
        markdown_path=markdown_path,
    )


def test_inventory_markdown_path_resolves_repo_and_container_layouts(tmp_path: Path) -> None:
    repo_module = tmp_path / "repo" / "ea" / "app" / "services" / "ltd_runtime_catalog.py"
    repo_module.parent.mkdir(parents=True, exist_ok=True)
    repo_root_inventory = repo_module.parents[3] / "LTDs.md"
    repo_root_inventory.write_text(_sample_ltd_markdown(), encoding="utf-8")
    assert _inventory_markdown_path(module_path=repo_module) == repo_root_inventory

    container_module = tmp_path / "app" / "app" / "services" / "ltd_runtime_catalog.py"
    container_module.parent.mkdir(parents=True, exist_ok=True)
    container_inventory = container_module.parents[2] / "LTDs.md"
    container_inventory.write_text(_sample_ltd_markdown(), encoding="utf-8")
    assert _inventory_markdown_path(module_path=container_module) == container_inventory


def test_browseract_ui_service_aliases_resolve_inventory_service_names() -> None:
    documentation = browseract_ui_service_by_alias("Documentation.AI")
    assert documentation is not None
    assert documentation.service_key == "documentation_ai_workspace_reader"

    poppy = browseract_ui_service_by_alias("Poppy AI")
    assert poppy is not None
    assert poppy.service_key == "poppy_workspace_reader"

    apixdrive = browseract_ui_service_by_alias("ApiX-Drive")
    assert apixdrive is not None
    assert apixdrive.service_key == "apixdrive_workspace_reader"

    assert browseract_ui_service_by_alias("BrowserAct") is None


def test_scope_excluded_inventory_does_not_enter_propertyquarry_catalog(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    assert catalog.get_profile("Chummer Only") is None


def _browseract_binding(
    *,
    principal_id: str = "principal-1",
    status: str = "enabled",
    services: tuple[str, ...] = ("Documentation.AI",),
) -> ConnectorBinding:
    return ConnectorBinding(
        binding_id="binding-1",
        principal_id=principal_id,
        connector_name="browseract",
        external_account_ref="browseract-account",
        scope_json={"services": list(services)},
        auth_metadata_json={},
        status=status,
        created_at="2026-08-13T00:00:00Z",
        updated_at="2026-08-13T00:00:00Z",
    )


def test_browseract_ltd_readiness_requires_principal_and_service_scope(
    tmp_path: Path,
) -> None:
    binding = _browseract_binding()
    assert browseract_binding_supports_service(
        binding,
        principal_id="principal-1",
        service_name="Documentation.AI",
    ) is True
    assert browseract_binding_supports_service(
        binding,
        principal_id="principal-2",
        service_name="Documentation.AI",
    ) is False
    assert browseract_binding_supports_service(
        binding,
        principal_id="principal-1",
        service_name="Poppy AI",
    ) is False
    assert browseract_binding_supports_service(
        _browseract_binding(status="disabled"),
        principal_id="principal-1",
        service_name="Documentation.AI",
    ) is False

    catalog = _catalog(tmp_path)
    documentation = catalog.get_profile("Documentation.AI")
    assert documentation is not None
    inspect_action = next(
        action
        for action in documentation.actions
        if action.action_key == "inspect_workspace"
    )
    container = SimpleNamespace(
        tool_runtime=SimpleNamespace(
            list_connector_bindings=lambda principal_id, limit=100: [binding]
        )
    )
    assert _browseract_action_ready_for_principal(
        container=container,
        principal_id="principal-1",
        service_name=documentation.service_name,
        action=inspect_action,
    ) is True

    crezlo = catalog.get_profile("Crezlo Tours")
    assert crezlo is not None
    crezlo_action = next(
        action
        for action in crezlo.actions
        if action.action_key == "create_property_tour"
    )
    assert _browseract_action_ready_for_principal(
        container=container,
        principal_id="principal-1",
        service_name=crezlo.service_name,
        action=crezlo_action,
    ) is False


def test_ltd_runtime_catalog_separates_contracts_from_live_evidence(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)

    onemin = catalog.get_profile("1min AI")
    assert onemin is not None
    assert onemin.runtime_state == "live_provider_evidence"
    assert onemin.evidence_status == "live_provider_call_verified"
    assert onemin.live_evidence_verified is True
    assert onemin.propertyquarry_customer_integration_verified is True
    assert onemin.matched_provider_key == "onemin"
    assert {action.action_key for action in onemin.actions} >= {
        "discover_account",
        "background_remove",
        "code_generate",
        "reasoned_patch_review",
        "image_generate",
        "image_upscale",
        "media_transform",
    }

    documentation = catalog.get_profile("Documentation.AI")
    assert documentation is not None
    assert documentation.runtime_state == "browseract_template_available"
    assert documentation.live_evidence_verified is False
    assert documentation.propertyquarry_customer_integration_verified is False
    assert documentation.browseract_ui_service_key == "documentation_ai_workspace_reader"
    assert {action.action_key for action in documentation.actions} == {
        "discover_account",
        "inspect_workspace",
    }
    assert all(action.executable is False for action in documentation.actions)

    markupgo = catalog.get_profile("markupgo")
    assert markupgo is not None
    assert markupgo.runtime_state == "browseract_template_available"
    assert markupgo.matched_provider_key == "markupgo"
    assert {action.action_key for action in markupgo.actions} == {
        "discover_account",
        "inspect_workspace",
    }

    poppy = catalog.get_profile("Poppy AI")
    assert poppy is not None
    assert poppy.runtime_state == "browseract_template_available"
    assert poppy.browseract_ui_service_key == "poppy_workspace_reader"
    assert poppy.matched_provider_key == "poppy_ai"
    assert {action.action_key for action in poppy.actions} == {
        "discover_account",
        "inspect_workspace",
    }

    emailit = catalog.get_profile("Emailit")
    assert emailit is not None
    assert emailit.runtime_state == "live_runtime_evidence"
    assert emailit.live_evidence_verified is True
    assert emailit.propertyquarry_customer_integration_verified is False
    assert {action.action_key for action in emailit.actions} == {
        "delivery_outbox",
        "discover_account",
    }

    fliplink = catalog.get_profile("FlipLink")
    assert fliplink is not None
    assert fliplink.runtime_state == "runtime_contract_available"
    assert fliplink.live_evidence_verified is False
    assert fliplink.propertyquarry_customer_integration_verified is False
    assert {action.action_key for action in fliplink.actions} == {
        "discover_account",
        "publish_property_flipbook",
    }
    flipbook_action = next(action for action in fliplink.actions if action.action_key == "publish_property_flipbook")
    assert flipbook_action.provider_key == "fliplink"
    assert flipbook_action.executable is False
    assert flipbook_action.input_schema_json["properties"]["privacy_mode"]["type"] == "string"

    hedy = catalog.get_profile("hedy.ai")
    assert hedy is not None
    assert hedy.runtime_state == "account_discovery_contract_available"
    assert [action.action_key for action in hedy.actions] == ["discover_account"]
    assert hedy.actions[0].executable is False

    crezlo = catalog.get_profile("Crezlo Tours")
    assert crezlo is not None
    crezlo_tour = next(
        action
        for action in crezlo.actions
        if action.action_key == "create_property_tour"
    )
    assert crezlo_tour.executable is False
    assert "customer-visible completion receipt" in crezlo_tour.notes
