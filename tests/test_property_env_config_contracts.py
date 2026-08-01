from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_example_lists_flagship_property_provider_switches() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "PROPERTYQUARRY_DEFAULT_BRAND" not in env

    for required in (
        "PROPERTYQUARRY_NEURONWRITER_ENABLED=0",
        "PROPERTYQUARRY_NEURONWRITER_REQUIRED=0",
        "PROPERTYQUARRY_NEURONWRITER_DOSSIER_MODE=public_only",
        "NEURONWRITER_API_KEY=",
        "PROPERTYQUARRY_DADAN_ENABLED=0",
        "PROPERTYQUARRY_DADAN_WEBHOOK_ALLOW_BASIC_AUTH=0",
        "DADAN_API_KEY=",
        "DADAN_WEBHOOK_SECRET=",
        "PROPERTYQUARRY_ID_AUSTRIA_REQUIRED=0",
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_ID=",
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET=",
        "PROPERTYQUARRY_ID_AUSTRIA_REDIRECT_URI=https://propertyquarry.com/id-austria/callback",
        "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET=",
        "PROPERTYQUARRY_ID_AUSTRIA_ENVIRONMENT=production",
        "PROPERTYQUARRY_ID_AUSTRIA_COMPLETION_DIR=_completion/id_austria",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ENABLED=0",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_ENABLED=0",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_DISABLED=0",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ALLOWED_HOSTS=",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_URL=",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BOOTSTRAP_EDGE=auto",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_ENABLED=0",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_ENABLED=0",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_URL=",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_ALLOWED_HOSTS=",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET=",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY=",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_COMPLETION_DIR=_completion/brilliant_directories",
        "MATTERPORT_API_KEY=",
        "PROPERTYQUARRY_MATTERPORT_LIVE_SMOKE=0",
        "PROPERTYQUARRY_TARGET_PROVIDER_MATRIX=0",
        "PROPERTYQUARRY_TARGET_PROVIDER_MATRIX_VARIANTS=strict,soft",
        "PROPERTYQUARRY_LIVE_PROVIDER_ALL_SEARCH_READY_COUNTRIES=0",
        "THREEDVISTA_LOGIN_EMAIL=",
        "THREEDVISTA_LICENSE_EMAIL=",
        "PROPERTYQUARRY_3DVISTA_EXPORT_ROOT=",
        "PROPERTYQUARRY_3DVISTA_LIVE_SMOKE=0",
        "MAGICFIT_EMAIL=",
        "MAGICFIT_PASSWORD=",
        "PROPERTYQUARRY_MAGICFIT_EMAIL=",
        "PROPERTYQUARRY_MAGICFIT_PASSWORD=",
        "PROPERTYQUARRY_MAGICFIT_LIVE_SMOKE=0",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED=0",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_PRINCIPAL_ID=",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_BASE_URL=https://propertyquarry.com",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_STATE=",
        "PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME=1",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED=0",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_PRINCIPAL_ID=",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_BASE_URL=https://propertyquarry.com",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_STATE=",
        "ONEMIN_AI_API_KEY=",
        "PROPERTYQUARRY_ONEMIN_LIVE_SMOKE=0",
        "JOGG_API_KEY=",
        "PROPERTYQUARRY_JOGG_LIVE_SMOKE=0",
        "PIXEFY_API_KEY=",
        "PROPERTYQUARRY_PIXEFY_LIVE_SMOKE=0",
        "RAFTER_API_KEY=",
        "PROPERTYQUARRY_RAFTER_LIVE_SMOKE=0",
        "PROPERTYQUARRY_3DVISTA_EXPORT_ROOT=/docker/property/state/public_property_tours/3dvista",
        "PROPERTYQUARRY_FASTESTVPN_ON_DEMAND_ENABLED=0",
        "PROPERTYQUARRY_FASTESTVPN_AUTO_STOP_AFTER_REFRESH=1",
        "PROPERTYQUARRY_RYBBIT_ENABLED=0",
        "PROPERTYQUARRY_RYBBIT_AUTHENTICATED_ENABLED=0",
        "PROPERTYQUARRY_RYBBIT_SKIP_PATTERNS=/workspace-access/**,/app/api/**,/v1/**,/api/**,/auth/**,/admin/**,/tours/files/**",
        "PROPERTYQUARRY_RYBBIT_MASK_PATTERNS=/app/**,/workspace-access/**,/app/handoffs/**,/tours/**,/app/properties/**,/app/research/**",
    ):
        assert required in env
    assert "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_PUBLIC_SITE_URL" not in env
    assert "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_PRICING_URL" not in env


def test_env_example_keeps_property_source_cache_inside_property_repo() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "EA_PROPERTY_SOURCE_LISTING_CACHE_PATH=/docker/property/state/property_source_listing_cache.json" in env
    assert "/docker/fleet/state/property_source_listing_cache.json" not in env
    assert "EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS=7776000" in env
    assert "search-run history is kept until the user deletes it" not in env


def test_property_public_tour_scripts_default_to_property_state() -> None:
    backfill = (ROOT / "scripts/backfill_public_tour_research_snapshots.py").read_text(encoding="utf-8")
    comparison = (ROOT / "scripts/build_comparison_dossiers.py").read_text(encoding="utf-8")
    crezlo_publish = (ROOT / "scripts/publish_crezlo_property_tours.py").read_text(encoding="utf-8")
    crezlo_public = (ROOT / "scripts/publish_crezlo_public_tours.py").read_text(encoding="utf-8")
    crezlo_worker = (ROOT / "scripts/crezlo_property_tour_worker.py").read_text(encoding="utf-8")

    for body in (backfill, comparison, crezlo_publish, crezlo_public):
        assert "/docker/property/state/public_property_tours" in body
        assert "/docker/fleet/state/public_property_tours" not in body
    for body in (crezlo_publish, crezlo_public, crezlo_worker):
        assert "PropertyQuarry-Crezlo" in body
        assert "EA-Crezlo" not in body
    assert "PROPERTYQUARRY_CREZLO_PLAYWRIGHT_IMAGE" in crezlo_worker
    assert "PROPERTYQUARRY_CREZLO_WORKSPACE_DOMAIN" in crezlo_worker
    assert "propertyquarry-tours.crezlotours.com" in crezlo_worker
    assert "/api/seller/tours/workspaces" in crezlo_worker
    assert "internal_fqdn" in crezlo_worker
    assert "tours-active-workspace" in crezlo_worker
    assert "workspace_create_permission_unavailable" in crezlo_worker
    assert "ea-property-tours" not in crezlo_worker
    assert "PropertyQuarry-hosted" in crezlo_publish
    assert "EA-hosted" not in crezlo_publish


def test_property_release_gate_runs_repair_fleet_canary() -> None:
    gate = (ROOT / "scripts/property_release_gates.sh").read_text(encoding="utf-8")

    assert "scripts/propertyquarry_repair_fleet_canary.py" in gate
    assert "scripts/verify_id_austria_provider.py" in gate
    assert "warning: Brilliant Directories verifier reported a blocked external billing lane" in gate


def test_property_compose_passes_id_austria_deployment_env() -> None:
    compose = (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")

    for key in (
        "PROPERTYQUARRY_ID_AUSTRIA_REQUIRED",
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_ID",
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET",
        "PROPERTYQUARRY_ID_AUSTRIA_REDIRECT_URI",
        "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET",
        "PROPERTYQUARRY_ID_AUSTRIA_ENVIRONMENT",
        "PROPERTYQUARRY_ID_AUSTRIA_ISSUER",
        "PROPERTYQUARRY_ID_AUSTRIA_AUTHORIZATION_ENDPOINT",
        "PROPERTYQUARRY_ID_AUSTRIA_TOKEN_ENDPOINT",
        "PROPERTYQUARRY_ID_AUSTRIA_JWKS_URI",
    ):
        assert key in compose


def test_property_compose_passes_brilliant_directories_deployment_env() -> None:
    compose = (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")

    for key in (
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ENABLED",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_ENABLED",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_DISABLED",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BASE_URL",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ALLOWED_HOSTS",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_URL",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_ENABLED",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_ENABLED",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_URL",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_ALLOWED_HOSTS",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY_HEADER",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_COMPLETION_DIR",
    ):
        assert key in compose
    assert "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_PUBLIC_SITE_URL" not in compose
    assert "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_PRICING_URL" not in compose


def test_env_example_keeps_external_investment_feeds_fail_closed_and_durable() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "EA_PROPERTY_INVESTMENT_EXTERNAL_ALLOWED_HOSTS=" in env
    assert "EA_PROPERTY_INVESTMENT_EXTERNAL_CACHE_PATH=/docker/property/state/property_investment_external_cache.json" in env
    assert "EA_PROPERTY_INVESTMENT_EXTERNAL_ALLOW_INSECURE_HTTP=1" not in env
    assert "/tmp/propertyquarry/state/property_investment_external_cache.json" not in env


def test_local_env_example_keeps_browseract_state_inside_property_repo() -> None:
    env = (ROOT / ".env.local.example").read_text(encoding="utf-8")

    assert "BROWSERACT_CHATPLAYGROUND_AUDIT_WORKFLOW_QUERY=propertyquarry_chatplayground_audit_live" in env
    assert (
        "BROWSERACT_CHATPLAYGROUND_AUDIT_RESULT_PATH="
        "/docker/property/state/browseract_bootstrap/runtime/propertyquarry_chatplayground_audit_live/result.json"
    ) in env
    assert "ea_chatplayground_audit_live" not in env
    assert "/docker/fleet" not in env


def test_release_hygiene_forbids_tracked_live_env_files() -> None:
    script = (ROOT / "scripts/check_property_release_hygiene.py").read_text(encoding="utf-8")

    assert 'tracked live env file forbidden' in script
    assert '".env"' in script
    assert '".env.local"' in script


def test_release_hygiene_binds_manifest_to_head_parent_or_metadata_only_ancestor() -> None:
    script = (ROOT / "scripts/check_property_release_hygiene.py").read_text(encoding="utf-8")

    assert "release_manifest_runtime_sha" in script
    assert "git_head_sha" in script
    assert "git_head_parent_sha" in script
    assert "git_commit_is_ancestor" in script
    assert "committed_paths_since" in script
    assert "RELEASE_METADATA_DESCENDANT_PATHS" in script
    assert "release manifest runtime commit is not HEAD, its parent, or a metadata-only ancestor" in script
    assert "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md" in script


def test_release_hygiene_requires_clean_releasable_worktree() -> None:
    script = (ROOT / "scripts/check_property_release_hygiene.py").read_text(encoding="utf-8")

    assert "_git_status_rows" in script
    assert "tracked worktree must be clean before release" in script
    assert "untracked release source files forbidden before release" in script
    assert '"tracked_worktree_clean"' in script
    assert '"no_untracked_release_source_files"' in script


def test_prod_compose_keeps_fastestvpn_repo_local_and_default_off() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "/docker/property" in compose
    assert "EA_FASTESTVPN_ON_DEMAND_ENABLED: ${PROPERTYQUARRY_FASTESTVPN_ON_DEMAND_ENABLED:-0}" in compose
    assert "${EA_FASTESTVPN_ON_DEMAND_ENABLED" not in compose
