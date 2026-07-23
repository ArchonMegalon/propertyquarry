from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import propertyquarry_runtime_isolation_v2 as isolation


RUNTIME_SHA = "a" * 40
DEPLOYMENT_ID = "9" * 64
WEB_IMAGE = f"ghcr.io/example/propertyquarry@sha256:{'b' * 64}"
RENDER_IMAGE = f"ghcr.io/example/propertyquarry-render@sha256:{'c' * 64}"
CLOUDFLARED_IMAGE = f"cloudflare/cloudflared@sha256:{'d' * 64}"
DATABASE_IMAGE = isolation.DATABASE_IMAGE
BACKUP_DIGEST = "sha256:" + "1" * 64
PURGE_DIGEST = "sha256:" + "2" * 64
RETIREMENT_DIGEST = "sha256:" + "3" * 64
IMAGE_IDS = {
    WEB_IMAGE: f"sha256:{'1' * 64}",
    RENDER_IMAGE: f"sha256:{'2' * 64}",
    CLOUDFLARED_IMAGE: f"sha256:{'3' * 64}",
    DATABASE_IMAGE: f"sha256:{'4' * 64}",
}


def _production_identity_stat(metadata: os.stat_result) -> os.stat_result:
    fields = list(metadata)
    fields[4] = 1000
    fields[5] = 1000
    return os.stat_result(fields)


class _RuntimeFixtureOS:
    """Expose one pytest tree as the production service identity."""

    def __init__(self, root: Path) -> None:
        self._root = os.path.abspath(os.fspath(root))

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    @staticmethod
    def _descriptor_path(descriptor: int) -> str:
        try:
            return os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            return ""

    def _resolved_path(
        self,
        path: object,
        *,
        dir_fd: int | None = None,
    ) -> str:
        if isinstance(path, int):
            return self._descriptor_path(path)
        raw = os.fsdecode(os.fspath(path))
        if dir_fd is not None and not os.path.isabs(raw):
            parent = self._descriptor_path(dir_fd)
            if parent:
                raw = os.path.join(parent, raw)
        return os.path.abspath(os.path.normpath(raw))

    def _is_fixture_path(
        self,
        path: object,
        *,
        dir_fd: int | None = None,
    ) -> bool:
        resolved = self._resolved_path(path, dir_fd=dir_fd)
        if not resolved:
            return False
        try:
            return os.path.commonpath((self._root, resolved)) == self._root
        except ValueError:
            return False

    def stat(self, path: object, *args: object, **kwargs: object) -> os.stat_result:
        metadata = os.stat(path, *args, **kwargs)
        if self._is_fixture_path(path, dir_fd=kwargs.get("dir_fd")):
            return _production_identity_stat(metadata)
        return metadata

    def lstat(self, path: object, *args: object, **kwargs: object) -> os.stat_result:
        metadata = os.lstat(path, *args, **kwargs)
        if self._is_fixture_path(path, dir_fd=kwargs.get("dir_fd")):
            return _production_identity_stat(metadata)
        return metadata

    def fstat(self, descriptor: int) -> os.stat_result:
        metadata = os.fstat(descriptor)
        if self._is_fixture_path(descriptor):
            return _production_identity_stat(metadata)
        return metadata

    @staticmethod
    def geteuid() -> int:
        return 0 if os.geteuid() == 0 else 1000

    @staticmethod
    def getegid() -> int:
        return 0 if os.getegid() == 0 else 1000


def _bind_runtime_fixture_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    if os.geteuid() in {0, 1000}:
        os.chown(tmp_path, 1000, 1000)
    facade = _RuntimeFixtureOS(tmp_path)
    monkeypatch.setattr(isolation, "os", facade)
    monkeypatch.setattr(pathlib, "os", facade)


def _runtime_retirement(
    *,
    receipt_root: Path | None = None,
    containers: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    root = receipt_root or isolation.RECEIPT_ROOT
    return {
        "containers": [] if containers is None else containers,
        "deployment_id": DEPLOYMENT_ID,
        "desired_live_allowlist": sorted(
            isolation.ALLOWED_PROPERTYQUARRY_CONTAINERS
        ),
        "operation": isolation.RETIREMENT_OPERATION,
        "preserve_volumes": True,
        "receipt_path": str(
            root
            / RUNTIME_SHA
            / DEPLOYMENT_ID
            / f"{isolation.RETIREMENT_OPERATION}.json"
        ),
    }


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(
            f"{key}={json.dumps(value, ensure_ascii=True)}\n"
            for key, value in values.items()
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    if os.geteuid() in {0, 1000}:
        os.chown(path, 1000, 1000)


def _mail_values() -> dict[str, str]:
    return {key: f"mail-value-{index}" for index, key in enumerate(isolation.MAIL_KEYS)}


def _legacy_mail_values(values: dict[str, str]) -> dict[str, str]:
    return {key: values[key] for key in isolation.LEGACY_MAIL_KEYS}


def _google_values() -> dict[str, str]:
    return {
        key: f"google-value-{index}"
        for index, key in enumerate(isolation.GOOGLE_KEYS)
    }


def test_registration_email_input_is_atomic_exact_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_runtime_fixture_metadata(tmp_path, monkeypatch)
    root_env = tmp_path / ".env"
    registration_env = tmp_path / "propertyquarry_registration_email.env"
    values = _mail_values()
    _write_env(root_env, {"UNRELATED": "keep-me", **values})
    monkeypatch.setattr(isolation, "ROOT_ENV", root_env)
    monkeypatch.setattr(isolation, "REGISTRATION_ENV", registration_env)
    scene_env = tmp_path / "scene.env"
    _write_env(scene_env, {"EXISTING": "keep"})
    monkeypatch.setattr(isolation, "SCENE_ENV", scene_env)

    first = isolation.prepare_registration_email_input()
    first_bytes = registration_env.read_bytes()
    second = isolation.prepare_registration_email_input()

    assert first["status"] == "prepared"
    assert first["key_count"] == len(isolation.MAIL_KEYS)
    assert first["registration_env_sha256"] == isolation._sha256_id(first_bytes)
    assert first["bridge_token_created"] is True
    assert second["bridge_token_created"] is False
    assert first["cloudflared_image_changed"] is True
    assert second["cloudflared_image_changed"] is False
    assert second["registration_env_sha256"] == first["registration_env_sha256"]
    assert registration_env.read_bytes() == first_bytes
    assert stat.S_IMODE(registration_env.stat().st_mode) == 0o600
    parsed, _raw = isolation._strict_env(
        registration_env,
        expected_keys=isolation.MAIL_KEYS,
    )
    assert parsed == values


def test_registration_email_input_accepts_legacy_eight_only_with_dedicated_ten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_runtime_fixture_metadata(tmp_path, monkeypatch)
    root_env = tmp_path / ".env"
    registration_env = tmp_path / "propertyquarry_registration_email.env"
    scene_env = tmp_path / "scene.env"
    values = _mail_values()
    _write_env(root_env, {"UNRELATED": "keep", **_legacy_mail_values(values)})
    _write_env(registration_env, values)
    _write_env(scene_env, {"EXISTING": "keep"})
    monkeypatch.setattr(isolation, "ROOT_ENV", root_env)
    monkeypatch.setattr(isolation, "REGISTRATION_ENV", registration_env)
    monkeypatch.setattr(isolation, "SCENE_ENV", scene_env)

    result = isolation.prepare_registration_email_input()

    root_values, _root_keys = isolation._parse_env(
        root_env.read_bytes(),
        path=root_env,
    )
    assert result["key_count"] == len(isolation.MAIL_KEYS) == 10
    assert result["legacy_source_present"] is True
    assert tuple(key for key in isolation.MAIL_KEYS if key in root_values) == (
        isolation.LEGACY_MAIL_KEYS
    )
    assert all(
        root_values[key] == values[key] for key in isolation.LEGACY_MAIL_KEYS
    )
    assert all(
        key not in root_values
        for key in (
            "PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN",
            "PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID",
        )
    )


def test_registration_email_input_rejects_partial_or_conflicting_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_runtime_fixture_metadata(tmp_path, monkeypatch)
    root_env = tmp_path / ".env"
    registration_env = tmp_path / "propertyquarry_registration_email.env"
    values = _mail_values()
    monkeypatch.setattr(isolation, "ROOT_ENV", root_env)
    monkeypatch.setattr(isolation, "REGISTRATION_ENV", registration_env)
    scene_env = tmp_path / "scene.env"
    _write_env(scene_env, {"EXISTING": "keep"})
    monkeypatch.setattr(isolation, "SCENE_ENV", scene_env)
    _write_env(root_env, {isolation.MAIL_KEYS[0]: values[isolation.MAIL_KEYS[0]]})

    with pytest.raises(isolation.IsolationError, match="legacy_registration_email_partial"):
        isolation.prepare_registration_email_input()

    _write_env(root_env, _legacy_mail_values(values))
    with pytest.raises(
        isolation.IsolationError,
        match="registration_email_source_missing",
    ):
        isolation.prepare_registration_email_input()

    _write_env(registration_env, values)
    mixed = {
        **_legacy_mail_values(values),
        "PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN": values[
            "PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN"
        ],
    }
    _write_env(root_env, mixed)
    with pytest.raises(
        isolation.IsolationError,
        match="legacy_registration_email_partial",
    ):
        isolation.prepare_registration_email_input()

    _write_env(root_env, values)
    conflicting = dict(values)
    conflicting[isolation.MAIL_KEYS[0]] = "different-secret"
    _write_env(registration_env, conflicting)
    with pytest.raises(isolation.IsolationError, match="registration_email_input_conflict"):
        isolation.prepare_registration_email_input()


def test_legacy_source_purge_preserves_every_unrelated_byte_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_runtime_fixture_metadata(tmp_path, monkeypatch)
    root_env = tmp_path / ".env"
    values = _mail_values()
    before = (
        b"# before\n"
        b"UNRELATED=keep#literal\n"
        + b"".join(
            f"{key}={json.dumps(value)}\n".encode("utf-8")
            for key, value in values.items()
        )
        + b"# after\nTAIL='still here'\n"
    )
    root_env.write_bytes(before)
    root_env.chmod(0o600)
    monkeypatch.setattr(isolation, "ROOT_ENV", root_env)

    removed, digest = isolation._purged_root_env()
    after = root_env.read_bytes()
    removed_again, digest_again = isolation._purged_root_env()

    assert removed == len(isolation.MAIL_KEYS)
    assert after == (
        b"# before\nUNRELATED=keep#literal\n# after\nTAIL='still here'\n"
    )
    assert digest == isolation._sha256_id(after)
    assert removed_again == 0
    assert digest_again == digest


def _mounts(name: str) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for mount_type, source, destination, writable in isolation.EXPECTED_MOUNTS[name]:
        result.append(
            {
                "Destination": destination,
                "Driver": "local" if mount_type == "volume" else "",
                "Mode": "rw" if writable else "ro",
                "Name": source if mount_type == "volume" else "",
                "Propagation": "" if mount_type == "volume" else "rprivate",
                "RW": writable,
                "Source": (
                    source
                    if mount_type == "bind"
                    else f"/var/lib/docker/volumes/{source}/_data"
                ),
                "Type": mount_type,
            }
        )
    return result


def _container(
    name: str,
    *,
    service: str,
    image: str,
    environment: dict[str, str],
    networks: set[str],
    state: dict[str, object] | None = None,
) -> dict[str, object]:
    process = isolation.EXPECTED_PROCESS_CONTRACT[name]
    (
        read_only,
        cap_drop,
        security_opt,
        pids_limit,
        restart_name,
        restart_count,
        cgroup_parent,
    ) = isolation.EXPECTED_HOST_CONTRACT[name]
    port_bindings: dict[str, object] = {}
    if name == "propertyquarry-api-live":
        port_bindings = {
            "8090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8097"}]
        }
    return {
        "Config": {
            "Cmd": process["cmd"],
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "Entrypoint": process["entrypoint"],
            "Healthcheck": process["healthcheck"],
            "Image": image,
            "Labels": {
                "com.docker.compose.project": "property",
                "com.docker.compose.service": service,
            },
            "User": process["user"],
        },
        "HostConfig": {
            "CapAdd": None,
            "CapDrop": cap_drop,
            "CgroupParent": cgroup_parent,
            "CgroupnsMode": "private",
            "DeviceCgroupRules": None,
            "DeviceRequests": None,
            "Devices": None,
            "IpcMode": "private",
            "Init": True,
            "NetworkMode": isolation.EXPECTED_NETWORK_MODES[name],
            "PidsLimit": pids_limit,
            "PidMode": "",
            "PortBindings": port_bindings,
            "PublishAllPorts": False,
            "Privileged": False,
            "ReadonlyRootfs": read_only,
            "RestartPolicy": {
                "MaximumRetryCount": restart_count,
                "Name": restart_name,
            },
            "Runtime": "runc",
            "SecurityOpt": security_opt,
            "UTSMode": "",
            "UsernsMode": "",
            "VolumesFrom": None,
        },
        "Id": hashlib.sha256(name.encode("ascii")).hexdigest(),
        "Image": IMAGE_IDS[image],
        "Mounts": _mounts(name) if name in isolation.EXPECTED_MOUNTS else [],
        "NetworkSettings": {
            "Networks": {network: {} for network in networks},
            "Ports": copy.deepcopy(port_bindings),
        },
        "State": state
        or (
            {"Health": {"Status": "healthy"}, "Status": "running"}
            if name in isolation.REQUIRED_HEALTH_CONTAINERS
            else {"Status": "running"}
        ),
    }


def _runtime_containers(
    mail: dict[str, str],
    google: dict[str, str],
    scene: dict[str, str],
) -> dict[str, dict[str, object]]:
    database = dict(
        line.split("=", 1)
        for line in _database_env_bytes().decode("ascii").splitlines()
    )
    runtime_contracts = {
        "propertyquarry-api-live": ("api", "PROPERTYQUARRY_API_DATABASE_URL"),
        "propertyquarry-worker-live": (
            "worker",
            "PROPERTYQUARRY_WORKER_DATABASE_URL",
        ),
        "propertyquarry-scheduler-live": (
            "scheduler",
            "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
        ),
        "propertyquarry-render-live": (
            "render-tools",
            "PROPERTYQUARRY_RENDER_DATABASE_URL",
        ),
    }
    root_authority = {
        "EA_API_TOKEN": "api-token",
        "EA_PROVIDER_SECRET_KEY": "provider-secret",
        "EA_SIGNING_SECRET": "signing-secret",
        "POSTGRES_PASSWORD": "postgres-owner-secret",
        "PROPERTYQUARRY_CF_TUNNEL_TOKEN": "tunnel-secret",
    }
    containers: dict[str, dict[str, object]] = {}
    for name, (service, kind) in isolation.EXPECTED_CONTAINERS.items():
        environment = {"PROPERTYQUARRY_RELEASE_COMMIT_SHA": RUNTIME_SHA}
        if name == "propertyquarry-api-live":
            environment.update(mail)
            environment.update(google)
            environment[isolation.RENDER_BRIDGE_TOKEN_KEY] = scene[
                isolation.RENDER_BRIDGE_TOKEN_KEY
            ]
            environment.update(
                {
                    "EA_API_TOKEN": root_authority["EA_API_TOKEN"],
                    "EA_PROVIDER_SECRET_KEY": root_authority[
                        "EA_PROVIDER_SECRET_KEY"
                    ],
                    "EA_SIGNING_SECRET": root_authority["EA_SIGNING_SECRET"],
                    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY": "",
                    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET": "",
                    "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET": "",
                    "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET": "",
                    "PROPERTYQUARRY_RELEASE_PROBE_SECRET": "",
                }
            )
        if name in {
            "propertyquarry-worker-live",
            "propertyquarry-scheduler-live",
        }:
            environment.update(
                {
                    "EA_API_TOKEN": root_authority["EA_API_TOKEN"],
                    "EA_SIGNING_SECRET": root_authority["EA_SIGNING_SECRET"],
                }
            )
        if name == "propertyquarry-scheduler-live":
            environment.update(
                {
                    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY": "",
                    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET": "",
                    "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET": "",
                    "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET": "",
                }
            )
        if name == "propertyquarry-db-live":
            environment["POSTGRES_PASSWORD"] = root_authority["POSTGRES_PASSWORD"]
        if name == "propertyquarry-cloudflared-live":
            environment["TUNNEL_TOKEN"] = root_authority[
                "PROPERTYQUARRY_CF_TUNNEL_TOKEN"
            ]
        if name == "propertyquarry-render-live":
            environment.update(scene)
            for key in isolation.SCENE_RENDER_BLANK_KEYS:
                environment[key] = ""
        if name in runtime_contracts:
            role, database_key = runtime_contracts[name]
            environment.update(
                {
                    "DATABASE_URL": database[database_key],
                    "EA_ROLE": role,
                    "EA_RUNTIME_MODE": "prod",
                }
            )
        if name == "propertyquarry-api-live":
            environment["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"] = database[
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"
            ]
            environment.update(
                {
                    "PROPERTYQUARRY_GOVERNED_RENDER_API_URL": (
                        "https://render-authority.invalid"
                    ),
                    "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN": (
                        scene["PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN"]
                    ),
                    "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN_FILE": "",
                    "PROPERTYQUARRY_GOVERNED_RENDER_ALLOWED_ORIGIN": (
                        "https://propertyquarry.com"
                    ),
                    "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET": (
                        "api-consent-authority"
                    ),
                    "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET_FILE": "",
                    "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_STORE_DIR": (
                        "/data/governed-render-consents"
                    ),
                    "PROPERTYQUARRY_GOVERNED_RENDER_LOCALE": "en-US",
                    "THREEDVISTA_LICENSE_EMAIL": "",
                    "THREEDVISTA_LOGIN_EMAIL": "",
                    "THREEDVISTA_LOGIN_PASSWORD": "",
                }
            )
            for key in (
                "PROPERTYQUARRY_API_DATABASE_URL",
                "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
                "PROPERTYQUARRY_RENDER_DATABASE_URL",
                "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
                "PROPERTYQUARRY_WORKER_DATABASE_URL",
                "POSTGRES_PASSWORD",
            ):
                environment[key] = ""
        if name in {
            "propertyquarry-api-live",
            "propertyquarry-worker-live",
            "propertyquarry-scheduler-live",
        }:
            environment["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"] = database[
                "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
            ]
        if name in {
            "propertyquarry-worker-live",
            "propertyquarry-scheduler-live",
        }:
            for key in isolation.SCENE_RENDER_BLANK_KEYS:
                environment[key] = ""
        if name == "propertyquarry-scheduler-live":
            for key in (
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
                "PROPERTYQUARRY_API_DATABASE_URL",
                "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
                "PROPERTYQUARRY_RENDER_DATABASE_URL",
                "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
                "PROPERTYQUARRY_WORKER_DATABASE_URL",
                "POSTGRES_PASSWORD",
            ):
                environment[key] = ""
        image = (
            WEB_IMAGE
            if kind == "web"
            else RENDER_IMAGE
            if kind == "render"
            else DATABASE_IMAGE if kind == "database" else CLOUDFLARED_IMAGE
        )
        containers[name] = _container(
            name,
            service=service,
            image=image,
            environment=environment,
            networks=isolation.EXPECTED_NETWORKS[name],
        )
    migrate_name = "propertyquarry-migrate-live"
    containers[migrate_name] = _container(
        migrate_name,
        service="propertyquarry-migrate",
        image=WEB_IMAGE,
        environment={
            "DATABASE_URL": database["PROPERTYQUARRY_MIGRATION_DATABASE_URL"],
            "EA_ROLE": "property-search-migrate",
            "EA_RUNTIME_MODE": "prod",
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": database[
                "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
            ],
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA": RUNTIME_SHA,
        },
        networks={"property_default"},
        state={"ExitCode": 0, "Status": "exited"},
    )
    containers["ea-api"] = {
        "Config": {
            "Env": ["EA_ROLE=api"],
            "Image": "ea@sha256:fixture",
            "Labels": {
                "com.docker.compose.project": "ea",
                "com.docker.compose.service": "ea-api",
            },
        },
        "HostConfig": {
            "IpcMode": "private",
            "NetworkMode": "ea_default",
            "PidMode": "",
            "VolumesFrom": None,
        },
        "Id": hashlib.sha256(b"ea-api").hexdigest(),
        "Mounts": [
            {
                "Destination": "/data",
                "Name": "ea_ea_artifacts",
                "RW": True,
                "Source": "",
                "Type": "volume",
            }
        ],
        "NetworkSettings": {"Networks": {"ea_default": {}}},
        "State": {"Status": "running"},
    }
    return containers


def _runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    _bind_runtime_fixture_metadata(tmp_path, monkeypatch)
    mail = _mail_values()
    google = _google_values()
    scene = {
        "BROWSERACT_API_KEY": "browser-secret",
        "DATABASE_URL": "postgresql://scene-only.invalid/render",
        "EA_TELEGRAM_BOT_TOKEN": "telegram-secret",
        "ONEMIN_AI_API_KEY": "onemin-secret",
        "OPENAI_API_KEY": "openai-secret",
        "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN": "blocked-governed-token",
        "PROPERTYQUARRY_GOVERNED_RENDER_LOCALE": "",
        "PROPERTYQUARRY_MAGIC_API_KEY": "magic-secret",
        "THREEDVISTA_LOGIN_PASSWORD": "blocked-desktop-password",
        isolation.RENDER_BRIDGE_TOKEN_KEY: "render-bridge-secret",
    }
    paths = {
        "ROOT_ENV": tmp_path / ".env",
        "REGISTRATION_ENV": tmp_path / "registration.env",
        "GOOGLE_ENV": tmp_path / "google.env",
        "SCENE_ENV": tmp_path / "scene.env",
        "DATABASE_ENV": tmp_path / "database.env",
        "ADMISSION_ENV": tmp_path / "admission.env",
    }
    _write_env(
        paths["ROOT_ENV"],
        {
            "PROPERTYQUARRY_GOVERNED_RENDER_ALLOWED_ORIGIN": (
                "https://propertyquarry.com"
            ),
            "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN": "api-compose-authority",
            "PROPERTYQUARRY_GOVERNED_RENDER_API_URL": (
                "https://render-authority.invalid"
            ),
            "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET": (
                "api-consent-authority"
            ),
            "PROPERTYQUARRY_GOVERNED_RENDER_LOCALE": "en-US",
            "EA_API_TOKEN": "api-token",
            "EA_PROVIDER_SECRET_KEY": "provider-secret",
            "EA_SIGNING_SECRET": "signing-secret",
            "PCLOUD_PASSWORD": "root-external-secret",
            "POSTGRES_PASSWORD": "postgres-owner-secret",
            "PROPERTYQUARRY_CF_TUNNEL_TOKEN": "tunnel-secret",
            "UNRELATED": "value",
        },
    )
    _write_env(paths["REGISTRATION_ENV"], mail)
    _write_env(paths["GOOGLE_ENV"], google)
    _write_env(paths["SCENE_ENV"], scene)
    paths["DATABASE_ENV"].write_bytes(_database_env_bytes())
    paths["DATABASE_ENV"].chmod(0o600)
    database_values = dict(
        line.split("=", 1)
        for line in _database_env_bytes().decode("ascii").splitlines()
    )
    _write_env(
        paths["ADMISSION_ENV"],
        {
            "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": database_values[
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"
            ]
        },
    )
    for constant, path in paths.items():
        monkeypatch.setattr(isolation, constant, path)
    monkeypatch.setattr(
        isolation,
        "_image_identities",
        lambda references: {reference: IMAGE_IDS[reference] for reference in references},
    )
    monkeypatch.setattr(
        isolation,
        "_property_volume_mountpoints",
        lambda: {
            name: f"/var/lib/docker/volumes/{name}/_data"
            for name in isolation.PROPERTY_VOLUMES
        },
    )
    return mail, google, scene


def _bind_fixture_propertyquarry_keys(
    monkeypatch: pytest.MonkeyPatch,
    containers: dict[str, dict[str, object]],
) -> None:
    digests: dict[str, str] = {}
    for name in (*isolation.EXPECTED_CONTAINERS, *isolation.EXPECTED_ONE_SHOT_CONTAINERS):
        environment = isolation._container_env(containers[name], name=name)
        digests[name] = isolation._propertyquarry_key_digest(environment)
    monkeypatch.setattr(isolation, "EXPECTED_PROPERTYQUARRY_ENV_KEY_DIGESTS", digests)


def _exposure(request: SimpleNamespace) -> dict[str, object]:
    return isolation._runtime_exposure(
        request,
        require_source_purged=True,
        cloudflared_image=CLOUDFLARED_IMAGE,
        database_image=DATABASE_IMAGE,
        api_host_ip="127.0.0.1",
        api_host_port=8097,
        api_container_port=8090,
    )


def _database_env_bytes() -> bytes:
    passwords = {
        "admission": "A" * 48,
        "api": "B" * 48,
        "migration": "C" * 48,
        "erasure": "D" * 48,
        "scheduler": "E" * 48,
        "worker": "F" * 48,
    }
    values = {
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": f"postgresql://propertyquarry_admission_runtime:{passwords['admission']}@propertyquarry-db:5432/propertyquarry_admission",
        "PROPERTYQUARRY_API_DATABASE_URL": f"postgresql://propertyquarry_api:{passwords['api']}@propertyquarry-db:5432/propertyquarry",
        "PROPERTYQUARRY_MIGRATION_DATABASE_URL": f"postgresql://propertyquarry_migrator:{passwords['migration']}@propertyquarry-db:5432/propertyquarry?options=-c%20role%3Dpropertyquarry_owner%20-c%20search_path%3Dpublic%2Cpg_catalog",
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": passwords["erasure"],
        "PROPERTYQUARRY_RENDER_DATABASE_URL": f"postgresql://propertyquarry_admission_runtime:{passwords['admission']}@propertyquarry-db:5432/propertyquarry_admission",
        "PROPERTYQUARRY_SCHEDULER_DATABASE_URL": f"postgresql://propertyquarry_scheduler:{passwords['scheduler']}@propertyquarry-db:5432/propertyquarry",
        "PROPERTYQUARRY_WORKER_DATABASE_URL": f"postgresql://propertyquarry_worker:{passwords['worker']}@propertyquarry-db:5432/propertyquarry",
    }
    return "".join(
        f"{key}={values[key]}\n" for key in isolation.DATABASE_ENV_KEYS
    ).encode("ascii")


def _migration_schema() -> dict[str, object]:
    def component(name: str) -> dict[str, object]:
        return {
            "applied_versions": [1],
            "component": name,
            "current_version": 1,
            "previous_version": 0,
        }

    return {
        "google_identity": component("propertyquarry_google_identity"),
        "kernel": component("ea_kernel"),
        "property_search": component("property_search"),
        "status": "migrated",
    }


def _readiness_schema() -> dict[str, object]:
    def component(name: str) -> dict[str, object]:
        return {
            "applied_versions": [1],
            "component": name,
            "current_version": 1,
            "ready": True,
            "reason": "ready",
            "required_version": 1,
        }

    return {
        "google_identity": component("propertyquarry_google_identity"),
        "kernel": component("ea_kernel"),
        "property_search": component("property_search"),
        "ready": True,
        "status": "ready",
    }


def _database_runtime_inputs(env_digest: str) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index, path in enumerate(isolation.RUNTIME_INPUTS):
        raw = _database_env_bytes() if index == 2 else f"input-{index}\n".encode()
        result.append(
            {
                "gid": 1000,
                "mode": 0o600,
                "path": str(path),
                "sha256": env_digest if index == 2 else isolation._sha256_id(raw),
                "size": len(raw),
                "uid": 1000,
            }
        )
    return result


def _database_substrate() -> dict[str, object]:
    return {
        "container_id": "6" * 64,
        "container_name": "propertyquarry-db-live",
        "database": "propertyquarry",
        "database_oid": 4242,
        "image": DATABASE_IMAGE,
        "image_id": IMAGE_IDS[DATABASE_IMAGE],
        "pgdata_volume": {
            "created_at": "2026-06-20T22:22:54+02:00",
            "driver": "local",
            "labels": {
                "com.docker.compose.project": "property",
                "com.docker.compose.volume": "propertyquarry_pgdata",
            },
            "mountpoint": (
                "/var/lib/docker/volumes/"
                "property_propertyquarry_pgdata/_data"
            ),
            "name": "property_propertyquarry_pgdata",
            "options": {},
            "scope": "local",
        },
        "repo_digest": isolation._canonical_repo_digest(DATABASE_IMAGE),
    }


def _database_payloads(now: int, env_digest: str) -> dict[str, dict[str, object]]:
    runtime_inputs = _database_runtime_inputs(env_digest)
    substrate = _database_substrate()
    payloads: dict[str, dict[str, object]] = {}
    for index, operation in enumerate(isolation.DATABASE_OPERATIONS):
        if operation == "provision-roles":
            result: dict[str, object] = {
                "credential_reused": False,
                "database_oid": 4242,
                "roles": list(isolation.DATABASE_ROLES),
            }
        else:
            result = {
                "credential_reused": True,
                "database_oid": 4242,
                "schema": (
                    _migration_schema()
                    if operation == "migrate-schema"
                    else _readiness_schema()
                ),
            }
        payloads[operation] = {
            "authority_digest": "sha256:authority",
            "backup_max_age_seconds": 3600,
            "backup_receipt_sha256": BACKUP_DIGEST,
            "database": "propertyquarry",
            "database_container": "propertyquarry-db-live",
            "database_image": DATABASE_IMAGE,
            "database_image_id": IMAGE_IDS[DATABASE_IMAGE],
            "database_repo_digest": isolation._canonical_repo_digest(
                DATABASE_IMAGE
            ),
            "database_substrate_after": copy.deepcopy(substrate),
            "database_substrate_before": copy.deepcopy(substrate),
            "deployment_id": DEPLOYMENT_ID,
            "docker_network": "property_default",
            "env_file": "/fixture/database.env",
            "env_file_sha256": env_digest,
            "finished_at_epoch": now - 80 + index * 10,
            "host_machine_id_digest": "sha256:machine",
            "operation": operation,
            "predecessor_receipt_sha256": RETIREMENT_DIGEST,
            "production_ready": False,
            "purge_receipt_sha256": PURGE_DIGEST,
            "receipt_authority_key_id": "sha256:key",
            "result": result,
            "retirement_receipt_sha256": RETIREMENT_DIGEST,
            "runtime_inputs": copy.deepcopy(runtime_inputs),
            "runtime_sha": RUNTIME_SHA,
            "schema": "propertyquarry.database-control-receipt.v2",
            "secret_values_emitted": False,
            "started_at_epoch": now - 89 + index * 10,
            "status": "verified",
            "transaction_started_at_epoch": now - 100,
            "web_image": WEB_IMAGE,
        }
    return payloads


def _database_wrapper(payload: dict[str, object], private: object) -> dict[str, object]:
    encoded = isolation._canonical_json(payload)
    signature = private.sign(  # type: ignore[attr-defined]
        isolation.DATABASE_SIGNATURE_DOMAIN
        + len(encoded).to_bytes(8, byteorder="big", signed=False)
        + encoded
    )
    return {
        "payload": payload,
        "signature": isolation.base64.urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("="),
        "signature_key_id": "sha256:key",
    }


def test_runtime_exposure_proves_identity_render_topology_and_exact_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail, google, scene = _runtime_files(tmp_path, monkeypatch)
    containers = _runtime_containers(mail, google, scene)
    _bind_fixture_propertyquarry_keys(monkeypatch, containers)
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
    )

    result = isolation._runtime_exposure(
        request,
        require_source_purged=True,
        cloudflared_image=CLOUDFLARED_IMAGE,
        database_image=DATABASE_IMAGE,
        api_host_ip="127.0.0.1",
        api_host_port=8097,
        api_container_port=8090,
    )

    assert result["topology_isolated"] is True
    assert result["legacy_registration_email_present"] is False
    assert result["registration_email_key_count"] == len(isolation.MAIL_KEYS)
    assert result["google_key_count"] == len(isolation.GOOGLE_KEYS)
    assert result["one_shot_containers"] == [
        {
            "compose_service": "propertyquarry-migrate",
            "container_id": hashlib.sha256(
                b"propertyquarry-migrate-live"
            ).hexdigest(),
            "exit_code": 0,
            "image": WEB_IMAGE,
            "image_id": IMAGE_IDS[WEB_IMAGE],
            "name": "propertyquarry-migrate-live",
            "networks": ["property_default"],
            "repo_digest": isolation._canonical_repo_digest(WEB_IMAGE),
            "status": "exited",
        }
    ]


def test_runtime_exposure_rejects_background_secret_and_cross_product_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail, google, scene = _runtime_files(tmp_path, monkeypatch)
    containers = _runtime_containers(mail, google, scene)
    _bind_fixture_propertyquarry_keys(monkeypatch, containers)
    scheduler_env = containers["propertyquarry-scheduler-live"]["Config"]["Env"]
    scheduler_env.append("ONEMIN_AI_API_KEY=leaked")
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
    )

    with pytest.raises(
        isolation.IsolationError,
        match="container_unexpected_secret_env_exposed",
    ):
        isolation._runtime_exposure(
                request,
                require_source_purged=True,
                cloudflared_image=CLOUDFLARED_IMAGE,
                database_image=DATABASE_IMAGE,
                api_host_ip="127.0.0.1",
                api_host_port=8097,
                api_container_port=8090,
        )

    scheduler_env.pop()
    scheduler_env.append("EMAILIT_API_KEY=")
    with pytest.raises(
        isolation.IsolationError,
        match="background_identity_env_exposed",
    ):
        _exposure(request)

    scheduler_env.pop()
    scheduler_env.append("OPENAI_API_KEY=")
    with pytest.raises(
        isolation.IsolationError,
        match="background_render_provider_env_exposed",
    ):
        _exposure(request)

    scheduler_env.pop()
    containers["ea-api"]["NetworkSettings"]["Networks"]["property_default"] = {}
    with pytest.raises(isolation.IsolationError, match="propertyquarry_network_shared"):
        isolation._runtime_exposure(
            request,
            require_source_purged=True,
            cloudflared_image=CLOUDFLARED_IMAGE,
            database_image=DATABASE_IMAGE,
            api_host_ip="127.0.0.1",
            api_host_port=8097,
            api_container_port=8090,
        )


def test_runtime_exposure_rejects_every_external_secret_path_name_and_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail, google, scene = _runtime_files(tmp_path, monkeypatch)
    base = _runtime_containers(mail, google, scene)
    _bind_fixture_propertyquarry_keys(monkeypatch, base)
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
    )
    protected = (
        isolation.PROPERTY_ROOT,
        isolation.ROOT_ENV,
        isolation.SCENE_ENV,
        isolation.DATABASE_ENV,
        isolation.ADMISSION_ENV,
        isolation.GOOGLE_ENV,
        isolation.REGISTRATION_ENV,
    )
    database = dict(
        line.split("=", 1)
        for line in _database_env_bytes().decode("ascii").splitlines()
    )
    for path in protected:
        for field in ("Source", "Destination"):
            containers = copy.deepcopy(base)
            mount = {
                "Destination": "/external-data",
                "Name": "",
                "RW": False,
                "Source": "/external-data",
                "Type": "bind",
            }
            mount[field] = str(path)
            containers["ea-api"]["Mounts"].append(mount)
            monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
            with pytest.raises(
                isolation.IsolationError,
                match="propertyquarry_bind_shared",
            ):
                _exposure(request)

    containers = copy.deepcopy(base)
    containers["ea-api"]["Mounts"].append(
        {
            "Destination": "/stolen-pgdata",
            "Name": "",
            "RW": True,
            "Source": (
                "/var/lib/docker/volumes/"
                "property_propertyquarry_pgdata/_data"
            ),
            "Type": "bind",
        }
    )
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="propertyquarry_volume_bind_shared",
    ):
        _exposure(request)

    for assignment in (
        f"{isolation.MAIL_KEYS[0]}={mail[isolation.MAIL_KEYS[0]]}",
        f"RENAMED_SECRET={google[isolation.GOOGLE_KEYS[1]]}",
        "RENAMED_PQ_DATABASE="
        + database["PROPERTYQUARRY_MIGRATION_DATABASE_URL"],
        "RENAMED_PQ_BRIDGE=" + scene[isolation.RENDER_BRIDGE_TOKEN_KEY],
        "PROPERTYQUARRY_UNEXPECTED_SECRET=untrusted",
    ):
        containers = copy.deepcopy(base)
        containers["ea-api"]["Config"]["Env"].append(assignment)
        monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
        with pytest.raises(
            isolation.IsolationError,
            match="propertyquarry_secret_exposed",
        ):
            _exposure(request)


def test_runtime_exposure_rejects_renamed_and_unexpected_background_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail, google, scene = _runtime_files(tmp_path, monkeypatch)
    base = _runtime_containers(mail, google, scene)
    _bind_fixture_propertyquarry_keys(monkeypatch, base)
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
    )
    containers = copy.deepcopy(base)
    containers["propertyquarry-scheduler-live"]["Config"]["Env"].append(
        f"RENAMED_SECRET={mail[isolation.MAIL_KEYS[0]]}"
    )
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_unexpected_secret_env_exposed",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["Config"]["Env"] = [
        (
            "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN=attacker-token"
            if item.startswith("PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN=")
            else item
        )
        for item in containers["propertyquarry-api-live"]["Config"]["Env"]
    ]
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_unexpected_secret_env_exposed",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-scheduler-live"]["Config"]["Env"].append(
        "PROPERTYQUARRY_UNEXPECTED_SECRET=unexpected"
    )
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_propertyquarry_env_keys_invalid",
    ):
        _exposure(request)


def test_runtime_exposure_rejects_missing_health_image_process_and_public_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail, google, scene = _runtime_files(tmp_path, monkeypatch)
    base = _runtime_containers(mail, google, scene)
    _bind_fixture_propertyquarry_keys(monkeypatch, base)
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
    )
    for name in isolation.REQUIRED_HEALTH_CONTAINERS:
        containers = copy.deepcopy(base)
        containers[name]["State"].pop("Health")
        monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
        with pytest.raises(isolation.IsolationError, match="container_health_missing"):
            _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-db-live"]["Config"]["Image"] = (
        "attacker/database@sha256:" + "9" * 64
    )
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(isolation.IsolationError, match="container_image_identity_invalid"):
        _exposure(request)


def test_runtime_exposure_binds_roles_database_authority_and_zero_secret_infra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail, google, scene = _runtime_files(tmp_path, monkeypatch)
    base = _runtime_containers(mail, google, scene)
    _bind_fixture_propertyquarry_keys(monkeypatch, base)
    database = dict(
        line.split("=", 1)
        for line in _database_env_bytes().decode("ascii").splitlines()
    )
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
    )

    for name, assignment, reason in (
        (
            "propertyquarry-api-live",
            "EA_ROLE=worker",
            "container_runtime_role_invalid",
        ),
        (
            "propertyquarry-worker-live",
            "DATABASE_URL=postgresql://wrong.invalid/propertyquarry",
            "container_database_role_invalid",
        ),
    ):
        containers = copy.deepcopy(base)
        key = assignment.split("=", 1)[0]
        environment = containers[name]["Config"]["Env"]
        containers[name]["Config"]["Env"] = [
            assignment if item.startswith(f"{key}=") else item
            for item in environment
        ]
        monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
        with pytest.raises(isolation.IsolationError, match=reason):
            _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["Config"]["Env"] = [
        (
            "PROPERTYQUARRY_MIGRATION_DATABASE_URL="
            + database["PROPERTYQUARRY_MIGRATION_DATABASE_URL"]
            if item.startswith("PROPERTYQUARRY_MIGRATION_DATABASE_URL=")
            else item
        )
        for item in containers["propertyquarry-api-live"]["Config"]["Env"]
    ]
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_unexpected_secret_env_exposed",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-worker-live"]["Config"]["Env"].append(
        "RENAMED_DATABASE_AUTHORITY="
        + database["PROPERTYQUARRY_MIGRATION_DATABASE_URL"]
    )
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_renamed_secret_env_exposed",
    ):
        _exposure(request)

    for name in ("propertyquarry-db-live", "propertyquarry-cloudflared-live"):
        containers = copy.deepcopy(base)
        containers[name]["Config"]["Env"].append("EMAILIT_API_KEY=")
        monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
        with pytest.raises(
            isolation.IsolationError,
            match="infrastructure_secret_env_exposed",
        ):
            _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["Config"]["Env"].append(
        "PCLOUD_PASSWORD=root-external-secret"
    )
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_external_product_env_exposed",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["Config"]["Env"].append(
        "RENAMED_ROOT_SECRET=root-external-secret"
    )
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_unexpected_secret_env_exposed",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["Config"]["Cmd"] = ["/bin/sh"]
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(isolation.IsolationError, match="container_process_contract_invalid"):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["HostConfig"]["PortBindings"] = {
        "8090/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8097"}]
    }
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_host_security_contract_invalid",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["HostConfig"]["PublishAllPorts"] = True
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_host_security_contract_invalid",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-api-live"]["NetworkSettings"]["Ports"][
        "9999/tcp"
    ] = [{"HostIp": "0.0.0.0", "HostPort": "9999"}]
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(
        isolation.IsolationError,
        match="container_network_ports_invalid",
    ):
        _exposure(request)

    containers = copy.deepcopy(base)
    containers["propertyquarry-render-live"]["Image"] = "sha256:" + "8" * 64
    monkeypatch.setattr(isolation, "_all_containers", lambda: containers)
    with pytest.raises(isolation.IsolationError, match="container_image_identity_invalid"):
        _exposure(request)


def test_signed_request_binds_exact_git_sha_operation_and_receipt_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_root = tmp_path / "receipts"
    receipt = (
        receipt_root
        / RUNTIME_SHA
        / DEPLOYMENT_ID
        / "verify-runtime-isolation.json"
    )
    monkeypatch.setattr(isolation, "RECEIPT_ROOT", receipt_root)
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 0)
    args = argparse.Namespace(
        operation="verify-runtime-isolation",
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        receipt=str(receipt),
    )

    assert isolation._validate_signed_args(args) == (
        "verify-runtime-isolation",
        receipt,
    )
    args.runtime_sha = "a" * 64
    with pytest.raises(isolation.IsolationError, match="runtime_sha_invalid"):
        isolation._validate_signed_args(args)


def _retirement_fixture_container(
    name: str,
    *,
    compose_project: str,
    compose_service: str,
    volume: str = "",
    container_id: str = "6" * 64,
) -> dict[str, object]:
    mounts: list[dict[str, object]] = []
    if volume:
        mounts.append(
            {
                "Destination": "/data",
                "Driver": "local",
                "Mode": "z",
                "Name": volume,
                "Propagation": "",
                "RW": True,
                "Source": f"/var/lib/docker/volumes/{volume}/_data",
                "Type": "volume",
            }
        )
    return {
        "Config": {
            "Image": f"fixture/{name}:latest",
            "Labels": {
                "com.docker.compose.project": compose_project,
                "com.docker.compose.service": compose_service,
            },
        },
        "Created": "2026-07-22T12:34:56.000000000Z",
        "Id": container_id,
        "Image": "sha256:" + "7" * 64,
        "Mounts": mounts,
        "NetworkSettings": {"Networks": {f"{compose_project}_default": {}}},
    }


def test_retirement_removes_only_signed_exact_targets_and_preserves_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_name = "propertyquarry-api"
    desired_name = "propertyquarry-db-live"
    volume_name = "propertyquarry-old-data"
    stale = _retirement_fixture_container(
        stale_name,
        compose_project="propertyquarry-old",
        compose_service="propertyquarry-api",
        volume=volume_name,
    )
    desired = _retirement_fixture_container(
        desired_name,
        compose_project="property",
        compose_service="propertyquarry-db",
        container_id="5" * 64,
    )
    signed = isolation._retirement_observation(stale_name, stale)
    retirement = _runtime_retirement(containers=[signed])
    removed = False

    def all_containers() -> dict[str, dict[str, object]]:
        result = {desired_name: desired}
        if not removed:
            result[stale_name] = stale
        return result

    volume_observation = {
        "created_at": "2026-07-22T12:34:56+02:00",
        "driver": "local",
        "labels": {"owner": "propertyquarry-old"},
        "mountpoint": f"/var/lib/docker/volumes/{volume_name}/_data",
        "name": volume_name,
        "options": {},
        "scope": "local",
    }

    def docker(*argv: str, timeout: int = 60) -> str:
        nonlocal removed
        assert argv == ("rm", "--force", "6" * 64)
        assert timeout == 120
        removed = True
        return f"{'6' * 64}\n"

    monkeypatch.setattr(isolation, "_all_containers", all_containers)
    monkeypatch.setattr(isolation, "_docker", docker)
    monkeypatch.setattr(
        isolation,
        "_volume_observation",
        lambda name: dict(volume_observation) if name == volume_name else None,
    )

    result = isolation._retire_stale_runtime(
        retirement,
        backup_receipt_sha256="sha256:" + "8" * 64,
    )

    assert removed is True
    assert set(result) == {
        "backup_receipt_sha256",
        "preserved_volumes",
        "retired_containers",
        "unknown_matches",
        "volumes_removed",
    }
    assert result["retired_containers"] == [signed]
    assert result["preserved_volumes"] == [volume_observation]
    assert result["unknown_matches"] == []
    assert result["volumes_removed"] is False


def test_retirement_refuses_unknown_or_changed_targets_before_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_name = "propertyquarry-api"
    stale = _retirement_fixture_container(
        stale_name,
        compose_project="propertyquarry-old",
        compose_service="propertyquarry-api",
    )
    signed = isolation._retirement_observation(stale_name, stale)
    retirement = _runtime_retirement(containers=[signed])
    surprise_name = "propertyquarry-surprise"
    surprise = _retirement_fixture_container(
        surprise_name,
        compose_project="propertyquarry-surprise",
        compose_service="propertyquarry-api",
    )
    monkeypatch.setattr(
        isolation,
        "_all_containers",
        lambda: {stale_name: stale, surprise_name: surprise},
    )
    monkeypatch.setattr(
        isolation,
        "_docker",
        lambda *_args, **_kwargs: pytest.fail("removal must not run"),
    )
    with pytest.raises(
        isolation.IsolationError,
        match="runtime_retirement_unknown_match",
    ):
        isolation._retire_stale_runtime(
            retirement,
            backup_receipt_sha256="sha256:" + "8" * 64,
        )

    changed = copy.deepcopy(stale)
    changed["Image"] = "sha256:" + "9" * 64
    monkeypatch.setattr(
        isolation,
        "_all_containers",
        lambda: {stale_name: changed},
    )
    with pytest.raises(
        isolation.IsolationError,
        match="runtime_retirement_identity_mismatch",
    ):
        isolation._retire_stale_runtime(
            retirement,
            backup_receipt_sha256="sha256:" + "8" * 64,
        )


def test_installed_contract_binds_cloudflared_and_scene_input_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_runtime_fixture_metadata(tmp_path, monkeypatch)
    scene = tmp_path / "scene.env"
    scene.write_bytes(b"SCENE=value\n")
    scene.chmod(0o600)
    monkeypatch.setattr(isolation, "SCENE_ENV", scene)
    monkeypatch.setattr(
        isolation.backup_contract,
        "_validate_request",
        lambda *_args, **_kwargs: None,
    )
    private = object()
    public = object()
    monkeypatch.setattr(
        isolation.backup_contract,
        "_load_receipt_authority",
        lambda *_args, **_kwargs: (private, public, "sha256:key"),
    )
    monkeypatch.setattr(
        isolation.backup_contract,
        "_installed_bindings",
        lambda *_args, **_kwargs: {
            "authority_digest": "sha256:authority",
            "runtime_retirement_digest": isolation._sha256_id(
                isolation._canonical_json(_runtime_retirement())
            ),
        },
    )
    scene_fields = {
        "scene_video_env_path": str(scene),
        "scene_video_env_mode": 0o600,
        "scene_video_env_uid": 1000,
        "scene_video_env_gid": 1000,
        "scene_video_env_digest": isolation._sha256_id(b"SCENE=value\n"),
    }
    runtime_fields = {
        "api_container_port": 8090,
        "api_host_ip": "127.0.0.1",
        "api_host_port": 8097,
        "cloudflared_image": CLOUDFLARED_IMAGE,
        "database_image": DATABASE_IMAGE,
        **scene_fields,
    }

    def fake_json(path: Path) -> tuple[dict[str, object], bytes, str]:
        _ = path
        document = {
            **runtime_fields,
            "runtime_retirement": _runtime_retirement(),
        }
        return document, b"{}", "0" * 64

    monkeypatch.setattr(isolation.backup_contract, "_load_json_file", fake_json)
    args = argparse.Namespace(
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        envelope_sha="e" * 64,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
        cloudflared_image=CLOUDFLARED_IMAGE,
        database_image=DATABASE_IMAGE,
        api_host_ip="127.0.0.1",
        api_host_port=8097,
        api_container_port=8090,
    )

    (
        request,
        bindings,
        runtime_retirement,
        observed_private,
        observed_public,
        key_id,
    ) = isolation._contract(args)

    assert request.runtime_sha == RUNTIME_SHA
    assert bindings == {
        "authority_digest": "sha256:authority",
        **runtime_fields,
        "runtime_retirement_digest": isolation._sha256_id(
            isolation._canonical_json(_runtime_retirement())
        ),
    }
    assert runtime_retirement == _runtime_retirement()
    assert observed_private is private
    assert observed_public is public
    assert key_id == "sha256:key"


def test_database_receipts_require_canonical_time_oid_env_and_result_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = private.public_key()
    database_env = Path("/fixture/database.env")
    receipt_root = Path("/fixture/receipts")
    monkeypatch.setattr(isolation, "DATABASE_ENV", database_env)
    monkeypatch.setattr(isolation, "DATABASE_RECEIPT_ROOT", receipt_root)
    monkeypatch.setattr(
        isolation.backup_contract,
        "_machine_id_digest",
        lambda _path: "sha256:machine",
    )
    env_raw = _database_env_bytes()
    env_digest = isolation._sha256_id(env_raw)
    now = int(isolation.time.time())
    base_payloads = _database_payloads(now, env_digest)
    receipt_bytes: dict[Path, bytes] = {}

    def install(
        payloads: dict[str, dict[str, object]],
        *,
        pretty_operation: str = "",
    ) -> None:
        receipt_bytes.clear()
        predecessor_digest = RETIREMENT_DIGEST
        for operation in isolation.DATABASE_OPERATIONS:
            payload = payloads[operation]
            payload["predecessor_receipt_sha256"] = predecessor_digest
            wrapper = _database_wrapper(payload, private)
            encoded = isolation._canonical_json(wrapper) + b"\n"
            if operation == pretty_operation:
                encoded = (
                    json.dumps(wrapper, indent=2, sort_keys=True).encode("utf-8")
                    + b"\n"
                )
            receipt_bytes[
                receipt_root
                / RUNTIME_SHA
                / DEPLOYMENT_ID
                / f"{operation}.json"
            ] = encoded
            predecessor_digest = isolation._sha256_id(encoded)

    def fake_read(path: Path, **_kwargs: object) -> bytes:
        if path == database_env:
            return env_raw
        return receipt_bytes[path]

    monkeypatch.setattr(isolation, "_read_regular", fake_read)
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        web_image=WEB_IMAGE,
    )
    bindings = {
        "authority_digest": "sha256:authority",
        "backup_max_age_seconds": 3600,
        "database_image": DATABASE_IMAGE,
        "database_substrate": _database_substrate(),
        "runtime_inputs": _database_runtime_inputs(env_digest),
        "transaction_started_at_epoch": now - 100,
    }

    def read_receipts(*, earliest_started_at: int) -> dict[str, object]:
        return isolation._database_receipts(
            request,
            bindings=bindings,
            public_key=public,
            key_id="sha256:key",
            earliest_started_at=earliest_started_at,
            expected_substrate=_database_substrate(),
            backup_digest=BACKUP_DIGEST,
            purge_digest=PURGE_DIGEST,
            retirement_digest=RETIREMENT_DIGEST,
        )
    install(copy.deepcopy(base_payloads))

    observed = read_receipts(earliest_started_at=now - 100)

    assert set(observed) == set(isolation.DATABASE_OPERATIONS)
    assert {
        item["database_oid"] for item in observed.values()  # type: ignore[union-attr]
    } == {4242}

    with pytest.raises(isolation.IsolationError, match="database_receipt_time_invalid"):
        read_receipts(earliest_started_at=now - 50)

    install(copy.deepcopy(base_payloads), pretty_operation="provision-roles")
    with pytest.raises(isolation.IsolationError, match="signed_receipt_invalid"):
        read_receipts(earliest_started_at=now - 100)

    adversarial = copy.deepcopy(base_payloads)
    adversarial["migrate-schema"]["result"]["database_oid"] = 99  # type: ignore[index]
    install(adversarial)
    with pytest.raises(
        isolation.IsolationError,
        match="database_receipt_oid_continuity_invalid",
    ):
        read_receipts(earliest_started_at=now - 100)

    adversarial = copy.deepcopy(base_payloads)
    harden_schema = adversarial["harden-runtime-acl"]["result"]["schema"]  # type: ignore[index]
    for field in ("google_identity", "kernel", "property_search"):
        harden_schema[field]["applied_versions"] = [1, 2]  # type: ignore[index]
        harden_schema[field]["current_version"] = 2  # type: ignore[index]
        harden_schema[field]["required_version"] = 2  # type: ignore[index]
    install(adversarial)
    with pytest.raises(
        isolation.IsolationError,
        match="database_receipt_component_continuity_invalid",
    ):
        read_receipts(earliest_started_at=now - 100)

    adversarial = copy.deepcopy(base_payloads)
    adversarial["provision-roles"]["started_at_epoch"] = -1
    install(adversarial)
    with pytest.raises(isolation.IsolationError, match="database_receipt_integer_invalid"):
        read_receipts(earliest_started_at=now - 100)

    adversarial = copy.deepcopy(base_payloads)
    adversarial["verify-schema-readiness"]["finished_at_epoch"] = now + 3600
    install(adversarial)
    with pytest.raises(isolation.IsolationError, match="database_receipt_time_invalid"):
        read_receipts(earliest_started_at=now - 100)

    adversarial = copy.deepcopy(base_payloads)
    adversarial["harden-runtime-acl"]["env_file_sha256"] = "sha256:" + "9" * 64
    install(adversarial)
    with pytest.raises(isolation.IsolationError, match="database_receipt_binding_invalid"):
        read_receipts(earliest_started_at=now - 100)

    adversarial = copy.deepcopy(base_payloads)
    adversarial["migrate-schema"]["result"]["unexpected"] = True  # type: ignore[index]
    install(adversarial)
    with pytest.raises(isolation.IsolationError, match="database_receipt_schema_result_invalid"):
        read_receipts(earliest_started_at=now - 100)


def test_database_environment_requires_exact_sorted_runtime_contract() -> None:
    valid = _database_env_bytes()

    assert set(isolation._database_environment(valid)) == set(
        isolation.DATABASE_ENV_KEYS
    )
    lines = valid.splitlines(keepends=True)
    lines[0], lines[1] = lines[1], lines[0]
    with pytest.raises(
        isolation.IsolationError,
        match="database_environment_shape_invalid",
    ):
        isolation._database_environment(b"".join(lines))


def test_signed_purge_recovers_idempotently_after_post_replace_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_runtime_fixture_metadata(tmp_path, monkeypatch)
    mail = _mail_values()
    google = _google_values()
    root_env = tmp_path / ".env"
    registration_env = tmp_path / "registration.env"
    google_env = tmp_path / "google.env"
    scene_env = tmp_path / "scene.env"
    database_env = tmp_path / "database.env"
    admission_env = tmp_path / "admission.env"
    _write_env(
        root_env,
        {
            "UNRELATED": "keep",
            isolation.CLOUDFLARED_IMAGE_KEY: isolation.CLOUDFLARED_IMAGE,
            isolation.API_HOST_BIND_KEY: "127.0.0.1",
            isolation.API_HOST_PORT_KEY: "8097",
            **_legacy_mail_values(mail),
        },
    )
    _write_env(registration_env, mail)
    _write_env(google_env, google)
    _write_env(scene_env, {"BROWSERACT_API_KEY": "provider-secret"})
    _write_env(database_env, {"DATABASE": "role-url"})
    _write_env(admission_env, {"ADMISSION": "role-url"})
    for constant, path in {
        "ROOT_ENV": root_env,
        "REGISTRATION_ENV": registration_env,
        "GOOGLE_ENV": google_env,
        "SCENE_ENV": scene_env,
        "DATABASE_ENV": database_env,
        "ADMISSION_ENV": admission_env,
    }.items():
        monkeypatch.setattr(isolation, constant, path)
    pre_digest = isolation._sha256_id(root_env.read_bytes())
    post_bytes, expected_removed = isolation._filtered_root_env(root_env.read_bytes())
    assert expected_removed == len(isolation.LEGACY_MAIL_KEYS)
    post_digest = isolation._sha256_id(post_bytes)
    receipt = tmp_path / "purge.json"
    rollback_root = tmp_path / "rollback"
    rollback_path = (
        rollback_root
        / RUNTIME_SHA
        / DEPLOYMENT_ID
        / "root-env.pre-purge.enc"
    )
    rollback_path.parent.mkdir(parents=True)
    rollback_path.write_bytes(b"encrypted-fixture")
    monkeypatch.setattr(isolation, "ROLLBACK_ROOT", rollback_root)
    request = SimpleNamespace(
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        envelope_sha="e" * 64,
        web_image=WEB_IMAGE,
        render_image=RENDER_IMAGE,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    runtime_paths = (
        root_env,
        scene_env,
        database_env,
        admission_env,
        google_env,
        registration_env,
    )
    monkeypatch.setattr(isolation, "RUNTIME_INPUTS", runtime_paths)

    def descriptors(root_raw: bytes) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for path in runtime_paths:
            raw = root_raw if path == root_env else path.read_bytes()
            result.append(
                {
                    "gid": 1000,
                    "mode": 0o600,
                    "path": str(path),
                    "sha256": isolation._sha256_id(raw),
                    "size": len(raw),
                    "uid": 1000,
                }
            )
        return result

    pre_runtime_inputs = descriptors(root_env.read_bytes())
    post_runtime_inputs = descriptors(post_bytes)
    bindings = {
        "authority_digest": "sha256:authority",
        "authority_signature_digest": "sha256:" + "1" * 64,
        "backup_max_age_seconds": 3600,
        "cloudflared_image": CLOUDFLARED_IMAGE,
        "config_digest": "sha256:" + "2" * 64,
        "database_image": DATABASE_IMAGE,
        "database_substrate_digest": "sha256:" + "3" * 64,
        "deployment_id": DEPLOYMENT_ID,
        "package_authority_key_id": "sha256:key",
        "package_manifest_digest": "sha256:" + "4" * 64,
        "package_manifest_signature_digest": "sha256:" + "5" * 64,
        "plan_digest": "sha256:" + "6" * 64,
        "post_purge_root_env_digest": post_digest,
        "pre_purge_root_env_digest": pre_digest,
        "pre_purge_runtime_inputs": pre_runtime_inputs,
        "runtime_deploy_digest": "sha256:" + "7" * 64,
        "runtime_inputs": post_runtime_inputs,
        "runtime_retirement_digest": "sha256:" + "8" * 64,
        "transaction_started_at_epoch": 1,
        "api_host_ip": "127.0.0.1",
        "api_host_port": 8097,
        "api_container_port": 8090,
    }
    artifact_state = {"preimage": root_env.read_bytes(), "digest": pre_digest}

    def load_artifact(
        *,
        runtime_sha: str,
        deployment_id: str,
        expected_pre_purge_digest: str,
    ):
        assert runtime_sha == RUNTIME_SHA
        assert deployment_id == DEPLOYMENT_ID
        if expected_pre_purge_digest != artifact_state["digest"]:
            raise isolation.IsolationError("rollback_artifact_binding_invalid")
        return artifact_state["preimage"], {"path": str(rollback_path)}

    monkeypatch.setattr(
        isolation,
        "_ensure_rollback_artifact",
        lambda **_kwargs: load_artifact(
            runtime_sha=RUNTIME_SHA,
            deployment_id=DEPLOYMENT_ID,
            expected_pre_purge_digest=pre_digest,
        ),
    )
    monkeypatch.setattr(isolation, "_load_rollback_artifact", load_artifact)
    monkeypatch.setattr(
        isolation,
        "_current_runtime_inputs",
        lambda: (
            pre_runtime_inputs
            if isolation._sha256_id(root_env.read_bytes()) == pre_digest
            else post_runtime_inputs
        ),
    )
    monkeypatch.setattr(
        isolation,
        "_validate_signed_args",
        lambda _args: ("purge-legacy-runtime-exposure", receipt),
    )
    monkeypatch.setattr(
        isolation,
        "_contract",
        lambda _args: (
            request,
            bindings,
            _runtime_retirement(),
            private,
            private.public_key(),
            "sha256:key",
        ),
    )
    monkeypatch.setattr(
        isolation,
        "_read_backup_receipt",
        lambda *_args, **_kwargs: ({}, "sha256:backup"),
    )
    monkeypatch.setattr(
        isolation,
        "_runtime_exposure",
        lambda *_args, **_kwargs: {"topology_isolated": True},
    )
    monkeypatch.setattr(
        isolation.backup_contract,
        "_machine_id_digest",
        lambda _path: "sha256:machine",
    )
    written: list[dict[str, object]] = []
    monkeypatch.setattr(
        isolation,
        "_write_receipt",
        lambda _path, wrapper: written.append(dict(wrapper)),
    )
    args = argparse.Namespace(pre_purge_root_env_digest=pre_digest)

    first = isolation.execute_signed(args)
    second = isolation.execute_signed(args)

    assert first["payload"]["result"]["legacy_keys_removed"] == len(
        isolation.LEGACY_MAIL_KEYS
    )
    assert first["payload"]["result"][
        "rollback_artifact_expected_removed_keys"
    ] == len(isolation.LEGACY_MAIL_KEYS)
    assert second["payload"]["result"]["legacy_keys_removed"] == 0
    assert first["payload"]["result"]["pre_purge_root_env_digest"] == pre_digest
    assert (
        second["payload"]["result"]["post_purge_root_env_digest"]
        == first["payload"]["result"]["post_purge_root_env_digest"]
    )
    assert len(written) == 2

    args.pre_purge_root_env_digest = "sha256:" + "9" * 64
    with pytest.raises(
        isolation.IsolationError,
        match="pre_purge_root_env_digest_invalid",
    ):
        isolation.execute_signed(args)

    args.pre_purge_root_env_digest = pre_digest
    monkeypatch.setattr(
        isolation,
        "_validate_signed_args",
        lambda _args: ("restore-legacy-runtime-exposure", tmp_path / "restore.json"),
    )
    restored = isolation.execute_signed(args)

    assert root_env.read_bytes() == artifact_state["preimage"]
    assert restored["payload"]["result"]["restored"] is True
    assert (
        restored["payload"]["result"]["restored_root_env_digest"]
        == pre_digest
    )
    assert len(written) == 3


@pytest.mark.parametrize(
    "mail_keys",
    (isolation.LEGACY_MAIL_KEYS, isolation.MAIL_KEYS),
)
def test_encrypted_rollback_artifact_recovers_pending_link_and_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mail_keys: tuple[str, ...],
) -> None:
    rollback_root = tmp_path / "rollback"
    parent = rollback_root / RUNTIME_SHA / DEPLOYMENT_ID
    parent.mkdir(parents=True)
    parent.chmod(0o700)
    monkeypatch.setattr(isolation, "ROLLBACK_ROOT", rollback_root)
    monkeypatch.setattr(
        isolation,
        "_rollback_parent",
        lambda _sha, _deployment_id: parent,
    )
    monkeypatch.setattr(
        isolation,
        "_rollback_master_key",
        lambda: (bytes(range(32)), "sha256:key"),
    )
    monkeypatch.setattr(isolation.os, "chown", lambda *_args, **_kwargs: None)
    original_read = isolation._read_regular

    def relaxed_read(path: Path, **kwargs: object) -> bytes:
        if path.is_relative_to(rollback_root):
            kwargs["uid"] = None
            kwargs["gid"] = None
        return original_read(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(isolation, "_read_regular", relaxed_read)
    preimage = b"UNRELATED=keep\n" + b"".join(
        f"{key}=secret-{index}\n".encode("ascii")
        for index, key in enumerate(mail_keys)
    )
    digest = isolation._sha256_id(preimage)

    recovered, evidence = isolation._ensure_rollback_artifact(
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        preimage=preimage,
        expected_pre_purge_digest=digest,
    )

    final = isolation._rollback_artifact_path(RUNTIME_SHA, DEPLOYMENT_ID)
    pending = parent / "root-env.pre-purge.pending.enc"
    assert recovered == preimage
    assert evidence["plaintext_sha256"] == digest
    assert final.stat().st_nlink == 1

    os.link(final, pending)
    assert final.stat().st_nlink == 2
    recovered_again, _evidence = isolation._ensure_rollback_artifact(
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        preimage=preimage,
        expected_pre_purge_digest=digest,
    )
    assert recovered_again == preimage
    assert not pending.exists()
    assert final.stat().st_nlink == 1

    final.unlink()
    pending.write_bytes(b"partial-ciphertext")
    pending.chmod(0o600)
    recovered_after_partial, _evidence = isolation._ensure_rollback_artifact(
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        preimage=preimage,
        expected_pre_purge_digest=digest,
    )
    assert recovered_after_partial == preimage
    assert final.exists()
    assert not pending.exists()

    with pytest.raises(
        isolation.IsolationError,
        match="rollback_artifact_binding_invalid",
    ):
        isolation._load_rollback_artifact(
            runtime_sha=RUNTIME_SHA,
            deployment_id=DEPLOYMENT_ID,
            expected_pre_purge_digest="sha256:" + "9" * 64,
        )


def test_isolation_signature_has_its_own_domain() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    payload = {"operation": "verify-runtime-isolation", "runtime_sha": RUNTIME_SHA}

    wrapper = isolation._sign_payload(
        payload,
        private_key=private,
        key_id="sha256:key",
    )

    encoded = isolation._canonical_json(payload)
    signature_text = str(wrapper["signature"])
    signature = isolation.base64.urlsafe_b64decode(
        signature_text + "=" * ((4 - len(signature_text) % 4) % 4)
    )
    private.public_key().verify(
        signature,
        isolation.SIGNATURE_DOMAIN
        + len(encoded).to_bytes(8, byteorder="big", signed=False)
        + encoded,
    )


def test_installed_layout_loads_the_signed_backup_executable(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "libexec"
    install_root.mkdir()
    isolation_executable = install_root / "propertyquarry-runtime-isolation-v2"
    backup_executable = install_root / "propertyquarry-predeploy-backup-v2"
    deploy_executable = install_root / "propertyquarry-runtime-deploy-v2"
    shutil.copyfile(Path(isolation.__file__), isolation_executable)
    shutil.copyfile(Path(isolation.backup_contract.__file__), backup_executable)
    shutil.copyfile(Path(isolation.deploy_contract.__file__), deploy_executable)

    completed = subprocess.run(
        [sys.executable, str(isolation_executable), "--help"],
        cwd=tmp_path,
        env={
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "",
            "TZ": "UTC",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "prepare-runtime-inputs" in completed.stdout
    assert "prepare-registration-email-input" in completed.stdout
    assert "purge-legacy-runtime-exposure" in completed.stdout
    assert "restore-legacy-runtime-exposure" in completed.stdout
    assert "verify-runtime-isolation" in completed.stdout
