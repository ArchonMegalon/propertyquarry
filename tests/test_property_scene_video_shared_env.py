from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    path = ROOT / "scripts" / "property_scene_video_shared_env.py"
    spec = importlib.util.spec_from_file_location("property_scene_video_shared_env", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_property_scene_video_shared_env_materializes_magicfit_and_magicai_bridge(tmp_path: Path) -> None:
    module = _load_script()
    property_env = tmp_path / ".env"
    chummer_env = tmp_path / "chummer.env"
    property_env.write_text("", encoding="utf-8")
    chummer_env.write_text(
        "\n".join(
            [
                "CHUMMER_EA_MAGICFIT_EMAIL=magicfit@example.test",
                "CHUMMER_EA_MAGICFIT_PASSWORD=magicfit-pass",
                "CHUMMER_EA_MAGICFIT_TIER=5",
                "CHUMMER_EA_MAGICAI_EMAIL=magicai@example.test",
                "CHUMMER_EA_MAGICAI_PASSWORD=magicai-pass",
                "CHUMMER_EA_MAGICAI_API_KEY=ak_test_primary",
                "MAGICAI_ACCOUNT_01_EMAIL=magic-slot@example.test",
                "MAGICAI_ACCOUNT_01_PASSWORD=magic-slot-pass",
                "MAGICAI_ACCOUNT_01_API_KEY=ak_test_slot",
                "POSTGRES_PASSWORD=pq-pass",
                "DATABASE_URL=postgresql://pq-user:pq-pass@db.internal/property",
                "EA_STORAGE_BACKEND=memory",
                "EA_TELEGRAM_BOT_TOKEN=tg-bot-token",
                "EA_TELEGRAM_BOT_REGISTRY_JSON={\"bots\":[\"propertyquarry\"]}",
                "EA_TELEGRAM_DEFAULT_PRINCIPAL_ID=cf-email:test@example.test",
                "EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT=1",
                "ONEMIN_AI_API_KEY=onemin-primary-key",
                "ONEMIN_DIRECT_API_KEYS_JSON_FILE=/secrets/onemin-keys.json",
                "ONEMIN_AI_API_KEY_FALLBACK_7=onemin-fallback-7",
                "BROWSERACT_API_KEY=browseract-primary-key",
                "BROWSERACT_API_KEY_FALLBACK_2=browseract-fallback-2",
                "WORKLLM_BASE_URL=https://girschele-workspace.workllm.io",
                "WORKLLM_EMAIL=workllm@example.test",
                "WORKLLM_PASSWORD=workllm-pass",
                "WORKLLM_LICENSE_TIER=4",
                "WORKLLM_PROVIDER_VERIFIED=false",
                "WORKLLM_RUNTIME_ENABLED=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "state" / "runtime" / "property_scene_video_shared.env"
    account_host_dir = tmp_path / "state" / "scene_video_provider_accounts"
    account_runtime_dir = PurePosixPath("/runtime/scene_video_provider_accounts")

    result = module.write_shared_env_file(
        output_path=output_path,
        source_env_files=(property_env, chummer_env),
        account_host_dir=account_host_dir,
        account_runtime_dir=account_runtime_dir,
    )

    rendered = output_path.read_text(encoding="utf-8")
    magicfit_accounts = account_host_dir / "propertyquarry-shared-magicfit-accounts.json"
    magicai_accounts = account_host_dir / "propertyquarry-shared-magicai-accounts.json"

    assert result["status"] == "pass"
    assert "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE='/runtime/scene_video_provider_accounts/propertyquarry-shared-magicfit-accounts.json'" in rendered
    assert "PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON='" in rendered
    assert "PROPERTYQUARRY_MAGICFIT_TIER='5'" in rendered
    assert "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE='/runtime/scene_video_provider_accounts/propertyquarry-shared-magicai-accounts.json'" in rendered
    assert "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON='" in rendered
    assert "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE='/runtime/scene_video_provider_accounts/propertyquarry-shared-magicai-accounts.json'" in rendered
    assert "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON='" in rendered
    assert "PROPERTYQUARRY_OMAGIC_API_KEY='ak_test_primary'" in rendered
    assert "PROPERTYQUARRY_OMAGIC_RENDER_COMMAND='python /app/scripts/render_magicai_model_upload_adapter.py'" in rendered
    assert "PROPERTYQUARRY_MAGIC_RENDER_COMMAND='python /app/scripts/render_magicai_model_upload_adapter.py'" in rendered
    assert "DATABASE_URL='postgresql://postgres:pq-pass@propertyquarry-db:5432/postgres'" in rendered
    assert "EA_STORAGE_BACKEND='postgres'" in rendered
    assert "EA_TELEGRAM_BOT_TOKEN='tg-bot-token'" in rendered
    assert "EA_TELEGRAM_BOT_REGISTRY_JSON='{\"bots\":[\"propertyquarry\"]}'" in rendered
    assert "EA_TELEGRAM_DEFAULT_PRINCIPAL_ID='cf-email:test@example.test'" in rendered
    assert "EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT='1'" in rendered
    assert "ONEMIN_AI_API_KEY='onemin-primary-key'" in rendered
    assert "ONEMIN_DIRECT_API_KEYS_JSON_FILE='/secrets/onemin-keys.json'" in rendered
    assert "ONEMIN_AI_API_KEY_FALLBACK_7='onemin-fallback-7'" in rendered
    assert "BROWSERACT_API_KEY='browseract-primary-key'" in rendered
    assert "BROWSERACT_API_KEY_FALLBACK_2='browseract-fallback-2'" in rendered
    assert "WORKLLM_BASE_URL='https://girschele-workspace.workllm.io'" in rendered
    assert "WORKLLM_EMAIL='workllm@example.test'" in rendered
    assert "WORKLLM_PASSWORD='workllm-pass'" in rendered
    assert "WORKLLM_LICENSE_TIER='4'" in rendered
    assert "WORKLLM_PROVIDER_VERIFIED='false'" in rendered
    assert "WORKLLM_RUNTIME_ENABLED='false'" in rendered
    assert result["providers"]["workllm"]["credentials_present"] is True
    assert result["providers"]["workllm"]["agent_id_present"] is False
    assert "PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED" not in rendered
    assert "PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT" not in rendered
    assert output_path.stat().st_mode & 0o777 == 0o600
    assert magicfit_accounts.stat().st_mode & 0o777 == 0o600
    assert magicai_accounts.stat().st_mode & 0o777 == 0o600
    assert json.loads(magicfit_accounts.read_text(encoding="utf-8"))[0]["email"] == "magicfit@example.test"
    assert len(json.loads(magicai_accounts.read_text(encoding="utf-8"))) == 2


def test_property_scene_video_shared_env_preserves_memory_without_database_url(tmp_path: Path) -> None:
    module = _load_script()
    source_env = tmp_path / "source.env"
    source_env.write_text("EA_STORAGE_BACKEND=memory\n", encoding="utf-8")
    output_path = tmp_path / "property_scene_video_shared.env"

    module.write_shared_env_file(
        output_path=output_path,
        source_env_files=(source_env,),
        account_host_dir=tmp_path / "accounts",
        account_runtime_dir=PurePosixPath("/runtime/accounts"),
    )

    rendered = output_path.read_text(encoding="utf-8")
    assert "EA_STORAGE_BACKEND='memory'" in rendered
    assert "DATABASE_URL=" not in rendered


def test_property_scene_video_shared_env_load_does_not_override_existing_values(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    env_path = tmp_path / "property_scene_video_shared.env"
    env_path.write_text(
        "\n".join(
            [
                "PROPERTYQUARRY_OMAGIC_API_KEY='ak_from_file'",
                "PROPERTYQUARRY_OMAGIC_RENDER_COMMAND='python /app/scripts/render_magicai_model_upload_adapter.py'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_OMAGIC_API_KEY", "ak_existing")

    applied = module.load_shared_env(env_path)

    assert "PROPERTYQUARRY_OMAGIC_API_KEY" not in applied
    assert applied["PROPERTYQUARRY_OMAGIC_RENDER_COMMAND"] == "python /app/scripts/render_magicai_model_upload_adapter.py"
    assert os.environ["PROPERTYQUARRY_OMAGIC_API_KEY"] == "ak_existing"


def test_property_scene_video_shared_env_load_normalizes_database_url_for_host_runtime(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    env_path = tmp_path / "property_scene_video_shared.env"
    env_path.write_text(
        "DATABASE_URL='postgresql://postgres:pw@ea-db:5432/ea'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_host_resolves", lambda hostname: False)
    monkeypatch.setattr(
        module,
        "_docker_container_ip_for_host_alias",
        lambda hostname: "192.168.48.9" if hostname == "ea-db" else "",
    )

    applied = module.load_shared_env(env_path, override=True)

    assert applied["DATABASE_URL"] == "postgresql://postgres:pw@192.168.48.9:5432/ea"
    assert os.environ["DATABASE_URL"] == "postgresql://postgres:pw@192.168.48.9:5432/ea"


def test_property_scene_video_shared_env_load_uses_configured_default_output_path(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    env_path = tmp_path / "scene-video-default.env"
    env_path.write_text(
        "PROPERTYQUARRY_OMAGIC_RENDER_COMMAND='python /app/scripts/render_magicai_model_upload_adapter.py'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE", str(env_path))

    applied = module.load_shared_env()

    assert applied["PROPERTYQUARRY_OMAGIC_RENDER_COMMAND"] == "python /app/scripts/render_magicai_model_upload_adapter.py"
    assert os.environ["PROPERTYQUARRY_OMAGIC_RENDER_COMMAND"] == "python /app/scripts/render_magicai_model_upload_adapter.py"


def test_property_scene_video_shared_env_passes_through_safe_omagic_runtime_flags(tmp_path: Path) -> None:
    module = _load_script()
    source_env = tmp_path / "source.env"
    source_env.write_text(
        "\n".join(
            [
                "CHUMMER_EA_MAGICAI_EMAIL=magicai@example.test",
                "CHUMMER_EA_MAGICAI_PASSWORD=magicai-pass",
                "CHUMMER_EA_MAGICAI_API_KEY=ak_test_primary",
                "PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED=1",
                "PROPERTYQUARRY_OMAGIC_TEMPLATE_VARIANT_ID=299",
                "PROPERTYQUARRY_OMAGIC_TEMPLATE_ARGUMENT_NAME=UserObject",
                "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS=cdn.example,media.example",
                "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_MAX_BYTES=104857600",
                "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_TIMEOUT_SECONDS=120",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "bridge.env"

    module.write_shared_env_file(
        output_path=output_path,
        source_env_files=(source_env,),
        account_host_dir=tmp_path / "accounts",
        account_runtime_dir=PurePosixPath("/runtime/accounts"),
    )

    rendered = output_path.read_text(encoding="utf-8")

    assert "PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED='1'" in rendered
    assert "PROPERTYQUARRY_OMAGIC_TEMPLATE_VARIANT_ID='299'" in rendered
    assert "PROPERTYQUARRY_OMAGIC_TEMPLATE_ARGUMENT_NAME='UserObject'" in rendered
    assert "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_ALLOWED_HOSTS='cdn.example,media.example'" in rendered
    assert "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_MAX_BYTES='104857600'" in rendered
    assert "PROPERTYQUARRY_MOOTION_REMOTE_VIDEO_TIMEOUT_SECONDS='120'" in rendered


def test_property_scene_video_shared_env_passes_through_dynamic_onemin_fallback_keys(tmp_path: Path) -> None:
    module = _load_script()
    source_env = tmp_path / "source.env"
    source_env.write_text(
        "\n".join(
            [
                "ONEMIN_AI_API_KEY_FALLBACK_11=onemin-fallback-11",
                "ONEMIN_AI_API_KEY_FALLBACK_27=onemin-fallback-27",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "bridge.env"

    module.write_shared_env_file(
        output_path=output_path,
        source_env_files=(source_env,),
        account_host_dir=tmp_path / "accounts",
        account_runtime_dir=PurePosixPath("/runtime/accounts"),
    )

    rendered = output_path.read_text(encoding="utf-8")

    assert "ONEMIN_AI_API_KEY_FALLBACK_11='onemin-fallback-11'" in rendered
    assert "ONEMIN_AI_API_KEY_FALLBACK_27='onemin-fallback-27'" in rendered


def test_property_scene_video_shared_env_collects_distinct_magicfit_accounts_across_sources(tmp_path: Path) -> None:
    module = _load_script()
    ea_env = tmp_path / "ea.env"
    chummer_env = tmp_path / "chummer.env"
    ea_env.write_text(
        "\n".join(
            [
                "CHUMMER_EA_MAGICFIT_EMAIL=ea-magicfit@example.test",
                "CHUMMER_EA_MAGICFIT_PASSWORD=ea-pass",
                "CHUMMER_EA_MAGICFIT_TIER=4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    chummer_env.write_text(
        "\n".join(
            [
                "CHUMMER_EA_MAGICFIT_EMAIL=runtime-magicfit@example.test",
                "CHUMMER_EA_MAGICFIT_PASSWORD=runtime-pass",
                "CHUMMER_EA_MAGICFIT_TIER=5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "bridge.env"
    account_host_dir = tmp_path / "accounts"

    module.write_shared_env_file(
        output_path=output_path,
        source_env_files=(ea_env, chummer_env),
        account_host_dir=account_host_dir,
        account_runtime_dir=PurePosixPath("/runtime/accounts"),
    )

    accounts = json.loads((account_host_dir / "propertyquarry-shared-magicfit-accounts.json").read_text(encoding="utf-8"))

    assert [row["email"] for row in accounts] == [
        "runtime-magicfit@example.test",
        "ea-magicfit@example.test",
    ]
    assert accounts[0]["label"] == "shared_magicfit_primary"
    assert accounts[1]["label"] == "shared_magicfit_02"
