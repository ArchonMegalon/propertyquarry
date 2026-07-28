from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts/deploy_propertyquarry.sh"


def _deploy() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def test_release_plane_has_no_github_actions_workflows() -> None:
    workflows = ROOT / ".github" / "workflows"
    assert not workflows.exists() or not any(
        path.suffix.lower() in {".yml", ".yaml"}
        for path in workflows.iterdir()
    )
    marker = (ROOT / ".github/NO_GITHUB_ACTIONS.md").read_text(encoding="utf-8")
    assert "GitHub as a source repository" in marker
    assert "local Docker host" in marker


def test_local_deploy_requires_clean_canonical_repository_role() -> None:
    deploy = _deploy()
    assert "scripts/check_property_repository_role.py" in deploy
    assert "--expected-repository ArchonMegalon/propertyquarry" in deploy
    assert "--expected-role canonical" in deploy
    assert "--expected-head-sha" in deploy
    assert "--require-clean-worktree" in deploy


def test_local_deploy_accepts_only_the_local_docker_daemon() -> None:
    deploy = _deploy()
    assert 'DOCKER_HOST:-unix:///var/run/docker.sock' in deploy
    assert '!= "unix:///var/run/docker.sock"' in deploy
    assert "A non-default Docker context is not allowed." in deploy
    assert "Only the local Docker Unix socket is release authority." in deploy


def test_local_deploy_requires_role_scoped_runtime_credentials() -> None:
    deploy = _deploy()
    assert 'builtin printf -v "${key}"' in deploy
    assert '/usr/bin/printf -v "${key}"' not in deploy
    assert "must be owned by the operator and mode 0600" in deploy
    for env_file in (
        "propertyquarry_database_roles.env",
        "propertyquarry_admission.env",
        "propertyquarry_auth.env",
        "propertyquarry_google_identity.env",
        "propertyquarry_render_bridge.env",
    ):
        assert env_file in deploy
    for key in (
        "PROPERTYQUARRY_API_DATABASE_URL",
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
        "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
        "PROPERTYQUARRY_WORKER_DATABASE_URL",
        "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
        "PROPERTYQUARRY_RENDER_DATABASE_URL",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN",
        "PROPERTYQUARRY_CF_TUNNEL_TOKEN",
    ):
        assert key in deploy


def test_local_deploy_builds_distinct_images_and_uses_ids_for_compose() -> None:
    deploy = _deploy()
    assert '_manifest_values(ROOT)["release_commit_sha"]' in deploy
    assert 'git merge-base --is-ancestor "${runtime_sha}" "${head_sha}"' in deploy
    assert 'short_sha="${runtime_sha:0:12}"' in deploy
    assert "propertyquarry-standalone-web-runtime:local-${short_sha}" in deploy
    assert "propertyquarry-standalone-render-runtime:local-${short_sha}" in deploy
    assert "docker image inspect" in deploy
    assert 'export PROPERTYQUARRY_WEB_IMAGE="${web_image}"' in deploy
    assert 'export PROPERTYQUARRY_RENDER_IMAGE="${render_image}"' in deploy
    assert 'PROPERTYQUARRY_RELEASE_IMAGE_DIGEST="${web_image}"' in deploy


def test_local_deploy_protects_project_scope_and_waits_for_health() -> None:
    deploy = _deploy()
    assert "Refusing --remove-orphans with unexpected project services" in deploy
    assert "--file docker-compose.property.yml" in deploy
    assert "--file docker-compose.cloudflared.yml" in deploy
    assert "up --detach --remove-orphans --wait --wait-timeout 420" in deploy


def test_local_deploy_writes_the_exact_candidate_receipt() -> None:
    deploy = _deploy()
    assert "scripts/propertyquarry_local_deployment_receipt.py" in deploy
    assert '--expected-commit "${runtime_sha}"' in deploy
    assert '--expected-web-image "${web_image}"' in deploy
    assert '--expected-render-image "${render_image}"' in deploy
    assert '--compose-project "${COMPOSE_PROJECT_NAME}"' in deploy
    assert '--local-origin "http://127.0.0.1:${local_port}"' in deploy


def test_local_preflight_never_builds_or_starts_containers() -> None:
    deploy = _deploy()
    assert "SKIP_BUILD == 0 && PREFLIGHT_ONLY == 0" in deploy
    assert "if ((PREFLIGHT_ONLY == 1)); then" in deploy


def test_old_remote_release_authority_is_not_in_deploy_entrypoint() -> None:
    deploy = _deploy()
    for forbidden in (
        "GITHUB_ACTIONS",
        "workflow_dispatch",
        "ACTIONS_ID_TOKEN",
        "PROPERTYQUARRY_RELEASE_RUNNER_LABEL",
        "PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST",
        "/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller",
        "self-hosted",
    ):
        assert forbidden not in deploy
