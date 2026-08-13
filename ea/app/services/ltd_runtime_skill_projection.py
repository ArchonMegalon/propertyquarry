from __future__ import annotations

from typing import Any

from app.domain.models import TaskContract, now_utc_iso
from app.services.ltd_runtime_catalog import LtdRuntimeAction, LtdRuntimeCatalogService, LtdRuntimeProfile

TASK_KEY_PREFIX = "ltd_runtime__"
_ONEMIN_MEDIA_ACTION_FEATURE_TYPES: dict[str, str] = {
    "background_remove": "BACKGROUND_REMOVER",
    "image_upscale": "IMAGE_UPSCALER",
}
_ONEMIN_MEDIA_FEATURE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("BACKGROUND_REMOVER", ("remove_background", "background_remover", "transparent_background", "cutout")),
    ("IMAGE_UPSCALER", ("upscale", "upscaler", "enhance_resolution", "sharpen_image")),
    ("SEARCH_AND_REPLACE", ("search_and_replace", "replace_object", "swap_object", "replace_item")),
    ("IMAGE_VARIATOR", ("variation", "variations", "reimagine", "alternate_version")),
    ("IMAGE_EDITOR", ("image_edit", "edit_image", "transform_image", "restyle_image", "recolor_image")),
)


def _normalize_lookup(value: object) -> str:
    text = str(value or "").strip().strip("`").lower()
    normalized = []
    last_was_sep = False
    for char in text:
        if char.isalnum():
            normalized.append(char)
            last_was_sep = False
            continue
        if not last_was_sep:
            normalized.append("_")
            last_was_sep = True
    return "".join(normalized).strip("_")


def _titleize(value: str) -> str:
    words = [part for part in _normalize_lookup(value).split("_") if part]
    return " ".join(word.capitalize() for word in words) or "Untitled"


def projected_task_key(service_name: str, action_key: str) -> str:
    return f"{TASK_KEY_PREFIX}{_normalize_lookup(service_name)}__{_normalize_lookup(action_key)}"


def _parse_projected_task_key(task_key: str) -> tuple[str, str] | None:
    normalized = str(task_key or "").strip()
    if not normalized.startswith(TASK_KEY_PREFIX):
        return None
    payload = normalized[len(TASK_KEY_PREFIX) :]
    service_key, separator, action_key = payload.partition("__")
    if not separator or not service_key or not action_key:
        return None
    return service_key, action_key


def _runtime_catalog(service: LtdRuntimeCatalogService | None = None) -> LtdRuntimeCatalogService:
    return service or LtdRuntimeCatalogService()


def _supported_action(action: LtdRuntimeAction) -> bool:
    if action.execution_mode != "tool_execution":
        return False
    if not action.executable:
        if action.provider_key != "browseract":
            return False
        if action.tool_name == "browseract.crezlo_property_tour":
            return False
    if action.tool_name == "browseract.extract_account_facts":
        return True
    if action.tool_name == "browseract.crezlo_property_tour":
        return True
    if action.tool_name == "provider.magixai.structured_generate":
        return True
    if action.tool_name == "provider.onemin.code_generate":
        return True
    if action.tool_name == "provider.onemin.reasoned_patch_review":
        return True
    if action.tool_name == "provider.onemin.image_generate":
        return True
    if action.tool_name == "provider.onemin.media_transform":
        return True
    return action.tool_name.startswith("browseract.")


def _provider_hint(action: LtdRuntimeAction, profile: LtdRuntimeProfile) -> str:
    provider_key = str(action.provider_key or "").strip().lower()
    if provider_key == "browseract":
        return "BrowserAct"
    if provider_key == "magixai":
        return "AI Magicx"
    if provider_key == "onemin":
        return "1min.AI"
    display_name = str(profile.matched_provider_display_name or "").strip()
    if display_name:
        return display_name
    return str(action.provider_key or "LTD Runtime").strip() or "LTD Runtime"


def _action_input_schema(action: LtdRuntimeAction) -> dict[str, object]:
    schema = dict(action.input_schema_json or {})
    properties = dict(schema.get("properties") or {})
    if action.tool_name in {
        "provider.magixai.structured_generate",
        "provider.onemin.code_generate",
        "provider.onemin.image_generate",
        "provider.onemin.media_transform",
    }:
        properties.setdefault("prompt", {"type": "string"})
        properties.setdefault("source_text", {"type": "string"})
        properties.setdefault("normalized_text", {"type": "string"})
        if action.tool_name == "provider.onemin.image_generate":
            properties.setdefault("n", {"type": "integer"})
            properties.setdefault("quality", {"type": "string"})
            properties.setdefault("output_format", {"type": "string"})
            properties.setdefault("size", {"type": "string"})
            properties.setdefault("aspect_ratio", {"type": "string"})
        if action.tool_name == "provider.onemin.media_transform":
            properties.setdefault("feature_type", {"type": "string"})
            properties.setdefault("prompt_object", {"type": "object"})
            properties.setdefault("image_url", {"type": "string"})
            properties.setdefault("output_format", {"type": "string"})
            properties.setdefault("search_prompt", {"type": "string"})
            properties.setdefault("background", {"type": "string"})
            properties.setdefault("size", {"type": "string"})
            properties.setdefault("n", {"type": "integer"})
            properties.setdefault("quality", {"type": "string"})
        schema["properties"] = properties
        required = [str(value or "").strip() for value in schema.get("required", []) if str(value or "").strip()]
        if not required:
            schema["required"] = []
    return schema


def _deliverable_type(profile: LtdRuntimeProfile, action: LtdRuntimeAction) -> str:
    return f"ltd_runtime_{_normalize_lookup(profile.service_name)}_{_normalize_lookup(action.action_key)}_packet"


def _runtime_policy_json(profile: LtdRuntimeProfile, action: LtdRuntimeAction, task_key: str) -> dict[str, object]:
    provider_hint = _provider_hint(action, profile)
    input_schema_json = _action_input_schema(action)
    return {
        "class": "low",
        "workflow_template": "tool_then_artifact",
        "pre_artifact_tool_name": action.tool_name,
        "brain_profile": "easy",
        "artifact_output_template": "",
        "skill_catalog_json": {
            "skill_key": task_key,
            "name": f"{profile.service_name} {_titleize(action.action_key)}",
            "description": action.description,
            "input_schema_json": input_schema_json,
            "provider_hints_json": {
                "primary": [provider_hint],
                "catalog": ["ltd-runtime"],
                "service_name": profile.service_name,
                "runtime_state": profile.runtime_state,
            },
            "tool_policy_json": {
                "allowed_tools": [action.tool_name, "artifact_repository"],
            },
            "tags": [
                "ltd-runtime",
                _normalize_lookup(profile.service_name),
                _normalize_lookup(action.action_key),
            ],
            "memory_reads": [],
            "memory_writes": [],
        },
    }


def project_task_contract(task_key: str, *, catalog: LtdRuntimeCatalogService | None = None) -> TaskContract | None:
    parsed = _parse_projected_task_key(task_key)
    if parsed is None:
        return None
    service_key, action_key = parsed
    runtime_catalog = _runtime_catalog(catalog)
    for profile in runtime_catalog.list_profiles():
        if _normalize_lookup(profile.service_name) != service_key:
            continue
        for action in profile.actions:
            if _normalize_lookup(action.action_key) != action_key:
                continue
            if not _supported_action(action):
                return None
            deliverable_type = _deliverable_type(profile, action)
            runtime_policy_json = _runtime_policy_json(profile, action, task_key)
            return TaskContract(
                task_key=task_key,
                deliverable_type=deliverable_type,
                default_risk_class="low",
                default_approval_class="none",
                allowed_tools=(action.tool_name, "artifact_repository"),
                evidence_requirements=(),
                memory_write_policy="none",
                budget_policy_json={"class": "low"},
                updated_at=now_utc_iso(),
                runtime_policy_json=runtime_policy_json,
            )
    return None


def projected_task_key_for_request(
    *,
    goal: str = "",
    input_json: dict[str, Any] | None = None,
    catalog: LtdRuntimeCatalogService | None = None,
) -> str:
    payload = dict(input_json or {})
    runtime_catalog = _runtime_catalog(catalog)
    explicit_service = ""
    for key in ("service_name", "ltd_service_name", "provider_service_name"):
        candidate = str(payload.get(key) or "").strip()
        if candidate:
            explicit_service = candidate
            break
    explicit_action = ""
    for key in ("action_key", "ltd_action_key"):
        candidate = str(payload.get(key) or "").strip()
        if candidate:
            explicit_action = candidate
            break
    profile = None
    if explicit_service:
        profile = runtime_catalog.get_profile(explicit_service)
    if profile is None:
        profile = _infer_profile_from_goal(goal=goal, catalog=runtime_catalog)
    if profile is None:
        return ""
    action = _resolve_action_for_request(profile=profile, explicit_action=explicit_action, goal=goal, input_json=payload)
    if action is None or not _supported_action(action):
        return ""
    return projected_task_key(profile.service_name, action.action_key)


def _infer_profile_from_goal(*, goal: str, catalog: LtdRuntimeCatalogService) -> LtdRuntimeProfile | None:
    normalized_goal = _normalize_lookup(goal)
    if not normalized_goal:
        return None
    candidates: list[tuple[int, LtdRuntimeProfile]] = []
    for profile in catalog.list_profiles():
        names = {profile.service_name, profile.matched_provider_display_name}
        for name in names:
            normalized_name = _normalize_lookup(name)
            if normalized_name and normalized_name in normalized_goal:
                candidates.append((len(normalized_name), profile))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1].service_name.lower()))
    return candidates[0][1]


def _resolve_action_for_request(
    *,
    profile: LtdRuntimeProfile,
    explicit_action: str,
    goal: str,
    input_json: dict[str, Any],
) -> LtdRuntimeAction | None:
    normalized_explicit_action = _normalize_lookup(explicit_action)
    if normalized_explicit_action:
        for action in profile.actions:
            if normalized_explicit_action == _normalize_lookup(action.action_key):
                return action

    available = {str(action.action_key or "").strip(): action for action in profile.actions if _supported_action(action)}
    if not available:
        return None

    inferred_media_feature_type = infer_onemin_media_feature_type(goal=goal, input_json=input_json)
    image_input_present = _image_input_present(input_json)
    if (
        "create_property_tour" in available
        and any(
            str(input_json.get(key) or "").strip()
            for key in (
                "property_json",
                "tour_title",
                "property_url",
                "source_virtual_tour_url",
                "scene_strategy",
            )
        )
    ):
        return available["create_property_tour"]
    if any(str(input_json.get(key) or "").strip() for key in ("page_url", "result_title", "survey_url")):
        for preferred in ("inspect_workspace", "read_results", "read_queue"):
            if preferred in available:
                return available[preferred]
    if any(key in input_json for key in ("requested_fields", "account_hints_json")) and "discover_account" in available:
        return available["discover_account"]
    if str(input_json.get("diff_text") or "").strip() and "reasoned_patch_review" in available:
        return available["reasoned_patch_review"]
    prompt_present = any(str(input_json.get(key) or "").strip() for key in ("prompt", "source_text", "normalized_text"))
    if inferred_media_feature_type and "media_transform" in available and (prompt_present or image_input_present):
        specialized_action_key = {
            "BACKGROUND_REMOVER": "background_remove",
            "IMAGE_UPSCALER": "image_upscale",
        }.get(inferred_media_feature_type, "")
        if specialized_action_key and specialized_action_key in available:
            return available[specialized_action_key]
        return available["media_transform"]
    if prompt_present and str(input_json.get("feature_type") or "").strip() and "media_transform" in available:
        return available["media_transform"]
    if prompt_present and "code_generate" in available and _goal_suggests(goal, ("code", "patch", "parser", "implement", "build")):
        return available["code_generate"]
    if prompt_present and "image_generate" in available and _goal_suggests(
        goal,
        ("image", "illustration", "visual", "poster", "logo", "icon", "banner", "thumbnail", "photo", "mockup"),
    ):
        return available["image_generate"]
    if prompt_present and "structured_generate" in available:
        return available["structured_generate"]

    normalized_goal = _normalize_lookup(goal)
    if normalized_goal:
        action_keyword_map = (
            ("discover_account", ("discover_account", "account_facts", "verify_account", "account_profile")),
            ("inspect_workspace", ("inspect_workspace", "workspace", "surface", "console")),
            ("read_queue", ("approval_queue", "queue", "approvals")),
            ("read_results", ("results", "survey_results")),
            ("create_property_tour", ("property_tour", "tour")),
            ("background_remove", ("background_remove", "remove_background", "cutout_background")),
            ("image_upscale", ("image_upscale", "upscale_image")),
            ("code_generate", ("code_generate", "write_code", "generate_code", "implement", "patch")),
            ("reasoned_patch_review", ("review_patch", "patch_review", "code_review")),
            ("image_generate", ("image_generate", "generate_image", "hero_image", "illustration", "mockup", "poster")),
            ("media_transform", ("media_transform", "transform_image", "remove_background", "upscale_image", "edit_image")),
            ("structured_generate", ("structured_generate", "summarize", "summary", "generate_json", "return_json")),
        )
        for action_key, tokens in action_keyword_map:
            if action_key not in available:
                continue
            if any(token in normalized_goal for token in tokens):
                return available[action_key]

    if len(available) == 1:
        return next(iter(available.values()))
    return available.get("discover_account")


def _goal_suggests(goal: str, tokens: tuple[str, ...]) -> bool:
    normalized_goal = _normalize_lookup(goal)
    if not normalized_goal:
        return False
    return any(_normalize_lookup(token) in normalized_goal for token in tokens)


def _image_input_present(input_json: dict[str, Any]) -> bool:
    prompt_object = dict(input_json.get("prompt_object") or {})
    for key in ("image_url", "imageUrl", "asset_url", "assetUrl", "source_image_url", "url"):
        if str(input_json.get(key) or prompt_object.get(key) or "").strip():
            return True
    return False


def infer_onemin_media_feature_type(
    *,
    goal: str = "",
    input_json: dict[str, Any] | None = None,
) -> str:
    payload = dict(input_json or {})
    explicit = str(payload.get("feature_type") or "").strip().upper()
    if explicit:
        return explicit
    action_key = str(payload.get("action_key") or payload.get("ltd_action_key") or "").strip().lower()
    if action_key and action_key in _ONEMIN_MEDIA_ACTION_FEATURE_TYPES:
        return _ONEMIN_MEDIA_ACTION_FEATURE_TYPES[action_key]
    if any(str(payload.get(key) or "").strip() for key in ("search_prompt", "replace_prompt")):
        return "SEARCH_AND_REPLACE"
    prompt_object = dict(payload.get("prompt_object") or {})
    if any(str(prompt_object.get(key) or "").strip() for key in ("search_prompt", "replace_prompt")):
        return "SEARCH_AND_REPLACE"
    normalized_goal = _normalize_lookup(goal)
    if not normalized_goal:
        return ""
    goal_tokens = {part for part in normalized_goal.split("_") if part}
    if "background" in goal_tokens and goal_tokens.intersection({"remove", "removed", "transparent", "cutout"}):
        return "BACKGROUND_REMOVER"
    if "upscale" in goal_tokens or goal_tokens.issuperset({"image", "upscaler"}):
        return "IMAGE_UPSCALER"
    if "replace" in goal_tokens and "background" not in goal_tokens:
        return "SEARCH_AND_REPLACE"
    if goal_tokens.intersection({"edit", "transform", "restyle", "recolor"}):
        return "IMAGE_EDITOR"
    for feature_type, tokens in _ONEMIN_MEDIA_FEATURE_HINTS:
        if any(_normalize_lookup(token) in normalized_goal for token in tokens):
            return feature_type
    return ""


def _iter_projected_contracts(
    *,
    catalog: LtdRuntimeCatalogService | None = None,
):
    runtime_catalog = _runtime_catalog(catalog)
    for profile in runtime_catalog.list_profiles():
        for action in profile.actions:
            if not _supported_action(action):
                continue
            task_key = projected_task_key(profile.service_name, action.action_key)
            contract = project_task_contract(task_key, catalog=runtime_catalog)
            if contract is not None:
                yield profile, action, contract


def _provider_hint_matches(profile: LtdRuntimeProfile, action: LtdRuntimeAction, provider_hint: str) -> bool:
    normalized_hint = _normalize_lookup(provider_hint)
    if not normalized_hint:
        return True
    if normalized_hint in {"ltd", "ltd_runtime", "ltdruntime"}:
        return True
    candidates = (
        profile.service_name,
        profile.matched_provider_key,
        profile.matched_provider_display_name,
        profile.browseract_ui_service_key,
        action.action_key,
        action.tool_name,
        _provider_hint(action, profile),
        *profile.aliases,
    )
    for candidate in candidates:
        normalized_candidate = _normalize_lookup(candidate)
        if normalized_candidate and (
            normalized_hint in normalized_candidate or normalized_candidate in normalized_hint
        ):
            return True
    return False


def list_projected_task_contracts(
    *,
    provider_hint: str = "",
    limit: int = 100,
    catalog: LtdRuntimeCatalogService | None = None,
) -> tuple[TaskContract, ...]:
    normalized_limit = max(1, int(limit or 100))
    contracts: list[TaskContract] = []
    for profile, action, contract in _iter_projected_contracts(catalog=catalog):
        if provider_hint and not _provider_hint_matches(profile, action, provider_hint):
            continue
        contracts.append(contract)
        if len(contracts) >= normalized_limit:
            break
    return tuple(contracts)
