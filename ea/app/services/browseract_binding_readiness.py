from __future__ import annotations

from app.domain.models import ConnectorBinding


def configured_browseract_service_names(
    *,
    binding_auth_metadata_json: dict[str, object],
    binding_scope_json: dict[str, object],
) -> tuple[str, ...]:
    """Return the LTD/service names explicitly authorized on a binding."""

    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            return
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(normalized)

    raw_accounts = binding_auth_metadata_json.get("service_accounts_json")
    if isinstance(raw_accounts, dict):
        for key, value in raw_accounts.items():
            if isinstance(value, dict) and any(
                field in value
                for field in ("tier", "plan", "account_email", "email", "status")
            ):
                add(key)
            elif key in {"service_name", "service", "name"}:
                add(value)
    elif isinstance(raw_accounts, list):
        for value in raw_accounts:
            if isinstance(value, dict):
                add(
                    value.get("service_name")
                    or value.get("service")
                    or value.get("name")
                )

    raw_scope_services = binding_scope_json.get("services")
    if isinstance(raw_scope_services, (list, tuple)):
        for value in raw_scope_services:
            add(value)
    elif isinstance(raw_scope_services, str):
        add(raw_scope_services)

    raw_scopes = binding_scope_json.get("scopes")
    if isinstance(raw_scopes, (list, tuple)):
        for value in raw_scopes:
            add(value)
    elif isinstance(raw_scopes, str):
        add(raw_scopes)
    return tuple(ordered)


def browseract_binding_supports_service(
    binding: ConnectorBinding | None,
    *,
    principal_id: str,
    service_name: str,
) -> bool:
    """Fail-closed readiness predicate shared by catalog projection and execution."""

    if binding is None:
        return False
    if str(binding.status or "").strip().lower() != "enabled":
        return False
    if str(binding.principal_id or "").strip() != str(principal_id or "").strip():
        return False
    if str(binding.connector_name or "").strip().lower() != "browseract":
        return False
    requested = str(service_name or "").strip().lower()
    if not requested:
        return False
    configured = {
        str(value or "").strip().lower()
        for value in configured_browseract_service_names(
            binding_auth_metadata_json=dict(binding.auth_metadata_json or {}),
            binding_scope_json=dict(binding.scope_json or {}),
        )
        if str(value or "").strip()
    }
    return requested in configured
