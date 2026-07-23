#!/usr/bin/env python3
"""Prepare and prove the PropertyQuarry-only production runtime boundary."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping, Sequence

try:  # Repository execution.
    from scripts import propertyquarry_predeploy_backup_v2 as backup_contract
except ImportError:  # Installed next to the signed backup executable.
    from importlib.machinery import SourceFileLoader
    import importlib.util

    _backup_path = next(
        (
            candidate
            for candidate in (
                Path(__file__).with_name("propertyquarry-predeploy-backup-v2"),
                Path(__file__).with_name("propertyquarry_predeploy_backup_v2.py"),
            )
            if candidate.is_file()
        ),
        Path(__file__).with_name("propertyquarry-predeploy-backup-v2"),
    )
    _backup_loader = SourceFileLoader(
        "propertyquarry_predeploy_backup_v2",
        str(_backup_path),
    )
    _backup_spec = importlib.util.spec_from_loader(
        _backup_loader.name,
        _backup_loader,
    )
    if _backup_spec is None:  # pragma: no cover - interpreter guard
        raise ImportError("installed_backup_module_unavailable")
    backup_contract = importlib.util.module_from_spec(_backup_spec)
    sys.modules[_backup_loader.name] = backup_contract
    _backup_loader.exec_module(backup_contract)

try:  # Repository execution.
    from scripts import propertyquarry_runtime_deploy_v2 as deploy_contract
except ImportError:  # Installed next to the signed deploy executable.
    from importlib.machinery import SourceFileLoader as _DeploySourceFileLoader
    import importlib.util as _deploy_importlib_util

    _deploy_path = next(
        (
            candidate
            for candidate in (
                Path(__file__).with_name("propertyquarry-runtime-deploy-v2"),
                Path(__file__).with_name("propertyquarry_runtime_deploy_v2.py"),
            )
            if candidate.is_file()
        ),
        Path(__file__).with_name("propertyquarry-runtime-deploy-v2"),
    )
    _deploy_loader = _DeploySourceFileLoader(
        "propertyquarry_runtime_deploy_v2",
        str(_deploy_path),
    )
    _deploy_spec = _deploy_importlib_util.spec_from_loader(
        _deploy_loader.name,
        _deploy_loader,
    )
    if _deploy_spec is None:  # pragma: no cover - interpreter guard
        raise ImportError("installed_deploy_module_unavailable")
    deploy_contract = _deploy_importlib_util.module_from_spec(_deploy_spec)
    sys.modules[_deploy_loader.name] = deploy_contract
    _deploy_loader.exec_module(deploy_contract)


SCHEMA = "propertyquarry.runtime-isolation-receipt.v2"
SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runtime-isolation-receipt-"
    b"signature.v2\x00"
)
SIGNED_OPERATIONS = (
    "purge-legacy-runtime-exposure",
    "retire-stale-propertyquarry-runtime",
    "restore-legacy-runtime-exposure",
    "verify-runtime-isolation",
)
ROOT_ENV = Path("/docker/property/.env")
PROPERTY_ROOT = Path("/docker/property")
RUNTIME_ROOT = Path("/docker/property/state/runtime")
SCENE_ENV = RUNTIME_ROOT / "property_scene_video_shared.env"
DATABASE_ENV = RUNTIME_ROOT / "propertyquarry_database_roles.env"
ADMISSION_ENV = RUNTIME_ROOT / "propertyquarry_admission.env"
GOOGLE_ENV = RUNTIME_ROOT / "propertyquarry_google_identity.env"
REGISTRATION_ENV = RUNTIME_ROOT / "propertyquarry_registration_email.env"
RUNTIME_INPUTS = (
    ROOT_ENV,
    SCENE_ENV,
    DATABASE_ENV,
    ADMISSION_ENV,
    GOOGLE_ENV,
    REGISTRATION_ENV,
)
RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/isolation-receipts"
)
ROLLBACK_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/isolation-rollback"
)
DATABASE_RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/database-receipts"
)
DOCKER_BIN = "/usr/bin/docker"
RUNTIME_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEPLOYMENT_ID_RE = SHA256_RE
SHA256_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_RE = re.compile(
    r"^[a-z0-9./_-]+(?::[a-z0-9._-]+)?@sha256:[0-9a-f]{64}$"
)
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$"
)
MAIL_KEYS = (
    "EMAILIT_API_KEY",
    "PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN",
    "PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID",
    "EA_REGISTRATION_EMAIL_FROM",
    "EA_REGISTRATION_EMAIL_NAME",
    "EA_REGISTRATION_EMAIL_FROM_FALLBACK",
    "EA_REGISTRATION_EMAIL_NAME_FALLBACK",
    "EA_REGISTRATION_EMAIL_FORCE_FALLBACK",
    "EA_EMAIL_DEFAULT_FROM",
    "EA_EMAIL_DEFAULT_NAME",
)
LEGACY_MAIL_KEYS = (
    "EMAILIT_API_KEY",
    "EA_REGISTRATION_EMAIL_FROM",
    "EA_REGISTRATION_EMAIL_NAME",
    "EA_REGISTRATION_EMAIL_FROM_FALLBACK",
    "EA_REGISTRATION_EMAIL_NAME_FALLBACK",
    "EA_REGISTRATION_EMAIL_FORCE_FALLBACK",
    "EA_EMAIL_DEFAULT_FROM",
    "EA_EMAIL_DEFAULT_NAME",
)
GOOGLE_KEYS = (
    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
    "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
    "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
    "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
)
MAIL_SECRET_KEYS = (
    "EMAILIT_API_KEY",
    "PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN",
)
GOOGLE_SECRET_KEYS = (
    "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
    "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
    "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
)
RENDER_BRIDGE_TOKEN_KEY = "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"
SCENE_RENDER_BLANK_KEYS = frozenset(
    {
        "PROPERTYQUARRY_GOVERNED_RENDER_API_URL",
        "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN",
        "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN_FILE",
        "PROPERTYQUARRY_GOVERNED_RENDER_ALLOWED_ORIGIN",
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET",
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET_FILE",
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_STORE_DIR",
        "PROPERTYQUARRY_GOVERNED_RENDER_LOCALE",
        "THREEDVISTA_LICENSE_EMAIL",
        "THREEDVISTA_LOGIN_EMAIL",
        "THREEDVISTA_LOGIN_PASSWORD",
    }
)
SCENE_API_AUTHORITY_KEYS = frozenset(
    {
        "PROPERTYQUARRY_GOVERNED_RENDER_API_URL",
        "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN",
        "PROPERTYQUARRY_GOVERNED_RENDER_ALLOWED_ORIGIN",
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET",
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_STORE_DIR",
        "PROPERTYQUARRY_GOVERNED_RENDER_LOCALE",
    }
)
SCENE_ROLE_OVERRIDE_KEYS = frozenset({"DATABASE_URL", "EA_ROLE", "EA_RUNTIME_MODE"})
CLOUDFLARED_IMAGE_KEY = "PROPERTYQUARRY_CLOUDFLARED_IMAGE_SHA256"
API_HOST_BIND_KEY = "EA_HOST_BIND"
API_HOST_PORT_KEY = "EA_HOST_PORT"
CLOUDFLARED_IMAGE = (
    "cloudflare/cloudflared@sha256:"
    "18626b1baac4450214535cd5bc40ef44c0635244d585ebf707749c22b6f3408f"
)
DATABASE_IMAGE = (
    "postgres:16-alpine@sha256:"
    "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
)
ROLLBACK_ARTIFACT_NAME = "propertyquarry-root-env-pre-purge"
ROLLBACK_ARTIFACT_KIND = "propertyquarry-runtime-isolation-rollback"
DATABASE_OPERATIONS = (
    "provision-roles",
    "migrate-schema",
    "harden-runtime-acl",
    "verify-schema-readiness",
)
ISOLATION_COMMON_BINDING_KEYS = (
    "authority_digest",
    "authority_signature_digest",
    "config_digest",
    "package_authority_key_id",
    "package_manifest_digest",
    "package_manifest_signature_digest",
    "plan_digest",
    "api_container_port",
    "api_host_ip",
    "api_host_port",
    "backup_max_age_seconds",
    "cloudflared_image",
    "database_image",
    "database_substrate_digest",
    "deployment_id",
    "pre_purge_runtime_inputs",
    "runtime_deploy_digest",
    "runtime_inputs",
    "runtime_retirement_digest",
    "transaction_started_at_epoch",
)
DATABASE_ENV_KEYS = (
    "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
    "PROPERTYQUARRY_API_DATABASE_URL",
    "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
    "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
    "PROPERTYQUARRY_RENDER_DATABASE_URL",
    "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
    "PROPERTYQUARRY_WORKER_DATABASE_URL",
)
DATABASE_ROLES = (
    "propertyquarry_owner",
    "propertyquarry_migrator",
    "propertyquarry_api",
    "propertyquarry_worker",
    "propertyquarry_scheduler",
)
DATABASE_PAYLOAD_KEYS = frozenset(
    {
        "authority_digest",
        "backup_max_age_seconds",
        "backup_receipt_sha256",
        "database",
        "database_container",
        "database_image",
        "database_image_id",
        "database_repo_digest",
        "database_substrate_after",
        "database_substrate_before",
        "deployment_id",
        "docker_network",
        "env_file",
        "env_file_sha256",
        "finished_at_epoch",
        "host_machine_id_digest",
        "operation",
        "predecessor_receipt_sha256",
        "production_ready",
        "purge_receipt_sha256",
        "receipt_authority_key_id",
        "result",
        "retirement_receipt_sha256",
        "runtime_inputs",
        "runtime_sha",
        "schema",
        "secret_values_emitted",
        "started_at_epoch",
        "status",
        "transaction_started_at_epoch",
        "web_image",
    }
)
DATABASE_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-database-receipt-signature.v2\x00"
)
EXPECTED_CONTAINERS = {
    "propertyquarry-api-live": ("propertyquarry-api", "web"),
    "propertyquarry-worker-live": ("propertyquarry-worker", "web"),
    "propertyquarry-scheduler-live": ("propertyquarry-scheduler", "web"),
    "propertyquarry-render-live": ("propertyquarry-render-tools", "render"),
    "propertyquarry-db-live": ("propertyquarry-db", "database"),
    "propertyquarry-cloudflared-live": ("propertyquarry-cloudflared", "ingress"),
}
REQUIRED_HEALTH_CONTAINERS = frozenset(
    {
        "propertyquarry-api-live",
        "propertyquarry-worker-live",
        "propertyquarry-scheduler-live",
        "propertyquarry-render-live",
        "propertyquarry-db-live",
    }
)
EXPECTED_ONE_SHOT_CONTAINERS = {
    "propertyquarry-migrate-live": "propertyquarry-migrate",
}
ALLOWED_PROPERTYQUARRY_CONTAINERS = frozenset(
    {*EXPECTED_CONTAINERS, *EXPECTED_ONE_SHOT_CONTAINERS}
)
RETIREMENT_OPERATION = "retire-stale-propertyquarry-runtime"
RETIREMENT_CONTAINER_KEYS = frozenset(
    {
        "compose_project",
        "compose_service",
        "container_id",
        "created_at",
        "image",
        "image_id",
        "mounts",
        "name",
        "networks",
    }
)
RETIREMENT_LEGACY_NAME_PATTERNS = (
    re.compile(
        r"^propertyquarry-(?:api|cloudflared|migrate|render-tools|scheduler|worker)$"
    ),
    re.compile(r"^propertyquarry-(?:api|db|migrate|render)-[0-9a-f]{8}$"),
    re.compile(r"^propertyquarry-admission-audit-[0-9a-f]{8}$"),
    re.compile(r"^propertyquarry-release-pin-(?:render|web)-[0-9]+$"),
    re.compile(r"^pq-ai-panorama-(?:canonical-strict|prater-preflight)$"),
)
RETIREMENT_MOUNT_KEYS = frozenset(
    {
        "destination",
        "driver",
        "mode",
        "name",
        "propagation",
        "rw",
        "source",
        "type",
    }
)
EXPECTED_NETWORKS = {
    "propertyquarry-api-live": {
        "property_default",
        "property_propertyquarry_render_internal",
    },
    "propertyquarry-worker-live": {"property_default"},
    "propertyquarry-scheduler-live": {"property_default"},
    "propertyquarry-render-live": {
        "property_propertyquarry_render_internal"
    },
    "propertyquarry-db-live": {
        "property_default",
        "property_propertyquarry_render_internal",
    },
    "propertyquarry-cloudflared-live": {"property_default"},
}
EXPECTED_NETWORK_MODES = {
    name: (
        "property_propertyquarry_render_internal"
        if name == "propertyquarry-render-live"
        else "property_default"
    )
    for name in ALLOWED_PROPERTYQUARRY_CONTAINERS
}
PROPERTY_NETWORKS = frozenset(
    {network for networks in EXPECTED_NETWORKS.values() for network in networks}
)
PROPERTY_VOLUME_PREFIX = "property_propertyquarry_"
PROPERTY_VOLUMES = frozenset(
    {
        "property_propertyquarry_pgdata",
        "property_propertyquarry_provider_ledger",
        "property_propertyquarry_artifacts",
        "property_propertyquarry_governed_render_consents",
        "property_propertyquarry_public_tours",
    }
)
EXPECTED_MOUNTS = {
    "propertyquarry-api-live": {
        ("bind", "/docker/property/config", "/config", False),
        ("bind", "/docker/property/config", "/app/config", False),
        (
            "bind",
            "/docker/property/state/incoming_property_tours",
            "/data/incoming_property_tours",
            True,
        ),
        ("volume", "property_propertyquarry_artifacts", "/data/artifacts", True),
        (
            "volume",
            "property_propertyquarry_governed_render_consents",
            "/data/governed-render-consents",
            True,
        ),
        (
            "volume",
            "property_propertyquarry_public_tours",
            "/data/public_property_tours",
            True,
        ),
        (
            "volume",
            "property_propertyquarry_provider_ledger",
            "/data/provider-ledger",
            True,
        ),
    },
    "propertyquarry-worker-live": {
        ("bind", "/docker/property/config", "/config", False),
        ("bind", "/docker/property/config", "/app/config", False),
        ("volume", "property_propertyquarry_artifacts", "/data/artifacts", True),
        (
            "volume",
            "property_propertyquarry_provider_ledger",
            "/data/provider-ledger",
            True,
        ),
    },
    "propertyquarry-scheduler-live": {
        ("bind", "/docker/property/config", "/config", False),
        ("bind", "/docker/property/config", "/app/config", False),
        (
            "bind",
            "/docker/property/state/incoming_property_tours",
            "/data/incoming_property_tours",
            True,
        ),
        ("volume", "property_propertyquarry_artifacts", "/data/artifacts", True),
        (
            "volume",
            "property_propertyquarry_public_tours",
            "/data/public_property_tours",
            True,
        ),
        (
            "volume",
            "property_propertyquarry_provider_ledger",
            "/data/provider-ledger",
            True,
        ),
    },
    "propertyquarry-render-live": {
        (
            "volume",
            "property_propertyquarry_public_tours",
            "/data/public_property_tours",
            True,
        ),
    },
    "propertyquarry-db-live": {
        (
            "volume",
            "property_propertyquarry_pgdata",
            "/var/lib/postgresql/data",
            True,
        ),
    },
    "propertyquarry-cloudflared-live": set(),
}
WEB_ENTRYPOINT = [
    "/usr/local/bin/python",
    "-I",
    "-S",
    "/usr/local/libexec/property_web_entrypoint.py",
]
WEB_COMMAND = ["/usr/local/bin/python", "-m", "app.runner"]
SCHEDULER_HEALTHCHECK = {
    "Test": [
        "CMD",
        "/usr/local/bin/python",
        "-m",
        "app.scheduler_healthcheck",
    ],
    "Interval": 30_000_000_000,
    "Timeout": 10_000_000_000,
    "StartPeriod": 90_000_000_000,
    "Retries": 5,
}
WEB_IMAGE_HEALTHCHECK = {
    "Test": [
        "CMD",
        "/bin/sh",
        "-ec",
        "role=${EA_ROLE:-api}; case \"$role\" in worker|scheduler|render-tools) exit 0 ;; *) exec /usr/local/bin/python -c 'import http.client; connection=http.client.HTTPConnection(\"127.0.0.1\",8090,timeout=10); connection.request(\"GET\",\"/health/live\"); response=connection.getresponse(); raise SystemExit(0 if response.status == 200 else 1)' ;; esac",
    ],
    "Interval": 30_000_000_000,
    "Timeout": 15_000_000_000,
    "StartPeriod": 30_000_000_000,
    "Retries": 5,
}
API_HEALTHCHECK = {
    "Test": [
        "CMD",
        "/usr/local/bin/python",
        "-I",
        "-S",
        "-c",
        "import http.client; connection = http.client.HTTPConnection('127.0.0.1', 8090, timeout=10); connection.request('GET', '/health/ready', headers={'Host': 'propertyquarry.com'}); response = connection.getresponse(); response.read(); connection.close(); raise SystemExit(0 if response.status == 200 else 1)",
    ],
    "Interval": 30_000_000_000,
    "Timeout": 15_000_000_000,
    "StartPeriod": 90_000_000_000,
    "Retries": 5,
}
RENDER_HEALTHCHECK = {
    "Test": [
        "CMD",
        "/usr/local/bin/property-render-env-launcher",
        "/usr/local/bin/python",
        "-I",
        "-S",
        "-c",
        "import http.client; connection = http.client.HTTPConnection('127.0.0.1', 8091, timeout=10); connection.request('GET', '/health/ready'); response = connection.getresponse(); response.read(); connection.close(); raise SystemExit(0 if response.status == 200 else 1)",
    ],
    "Interval": 30_000_000_000,
    "Timeout": 15_000_000_000,
    "StartPeriod": 30_000_000_000,
    "Retries": 5,
}
DATABASE_HEALTHCHECK = {
    "Test": ["CMD-SHELL", "pg_isready -U postgres -d propertyquarry"],
    "Interval": 30_000_000_000,
    "Timeout": 5_000_000_000,
    "Retries": 3,
}
EXPECTED_PROCESS_CONTRACT = {
    "propertyquarry-api-live": {
        "cmd": WEB_COMMAND,
        "entrypoint": WEB_ENTRYPOINT,
        "healthcheck": API_HEALTHCHECK,
        "user": "10001:10001",
    },
    "propertyquarry-worker-live": {
        "cmd": WEB_COMMAND,
        "entrypoint": WEB_ENTRYPOINT,
        "healthcheck": SCHEDULER_HEALTHCHECK,
        "user": "10001:10001",
    },
    "propertyquarry-scheduler-live": {
        "cmd": WEB_COMMAND,
        "entrypoint": WEB_ENTRYPOINT,
        "healthcheck": SCHEDULER_HEALTHCHECK,
        "user": "10001:10001",
    },
    "propertyquarry-render-live": {
        "cmd": [
            "/usr/local/bin/python",
            "-I",
            "/app/scripts/property_reconstruction_render_bridge.py",
        ],
        "entrypoint": [
            "/usr/local/bin/property-render-env-launcher",
            "/usr/local/bin/python",
            "-I",
            "-S",
            "/usr/local/libexec/property_render_entrypoint.py",
        ],
        "healthcheck": RENDER_HEALTHCHECK,
        "user": "10001:10001",
    },
    "propertyquarry-db-live": {
        "cmd": ["postgres"],
        "entrypoint": ["docker-entrypoint.sh"],
        "healthcheck": DATABASE_HEALTHCHECK,
        "user": "",
    },
    "propertyquarry-cloudflared-live": {
        "cmd": ["tunnel", "run"],
        "entrypoint": ["cloudflared", "--no-autoupdate"],
        "healthcheck": None,
        "user": "65532:65532",
    },
    "propertyquarry-migrate-live": {
        "cmd": [
            "/usr/local/bin/python",
            "-m",
            "app.product.propertyquarry_schema",
            "migrate",
        ],
        "entrypoint": WEB_ENTRYPOINT,
        "healthcheck": WEB_IMAGE_HEALTHCHECK,
        "user": "10001:10001",
    },
}
EXPECTED_HOST_CONTRACT = {
    "propertyquarry-api-live": (False, ["ALL"], ["no-new-privileges:true"], 128, "on-failure", 3, "system.slice"),
    "propertyquarry-worker-live": (True, ["ALL"], ["no-new-privileges:true"], 128, "on-failure", 3, "system.slice"),
    "propertyquarry-scheduler-live": (False, ["ALL"], ["no-new-privileges:true"], 128, "on-failure", 3, "system.slice"),
    "propertyquarry-render-live": (True, ["ALL"], ["no-new-privileges:true"], 256, "on-failure", 3, "system.slice"),
    "propertyquarry-db-live": (False, None, None, 128, "on-failure", 3, "system.slice"),
    "propertyquarry-cloudflared-live": (True, ["ALL"], ["no-new-privileges:true"], 64, "on-failure", 3, ""),
    "propertyquarry-migrate-live": (False, ["ALL"], ["no-new-privileges:true"], 64, "no", 0, "system.slice"),
}
EXPECTED_PROPERTYQUARRY_ENV_KEY_DIGESTS = {
    "propertyquarry-api-live": "sha256:44132dbd10c1ec63f6fa28b8ba91109e669253a9edd54fcc575266d2562f1e49",
    "propertyquarry-worker-live": "sha256:5455999499586a4ae2dccfb12e6ce0b2e4870281f53e0ce6f7f4d4619323b9a8",
    "propertyquarry-scheduler-live": "sha256:e8b814be0189afa7d114394cb9d6140edd4631c92491d5ff998c1213545e6d73",
    "propertyquarry-render-live": "sha256:6702ebcfdfe2de8ad89f53762cf7b112be3c2924e369dd6b2f8cb0d1812151e2",
    "propertyquarry-db-live": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "propertyquarry-cloudflared-live": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "propertyquarry-migrate-live": "sha256:cf88ae9ae34fe03773720c2451089d7d47a1b48b3f80c1cd12586d610e770bc6",
}
MAX_ENV_BYTES = 256 * 1024
MAX_ROLLBACK_BYTES = MAX_ENV_BYTES + (2 * 1024 * 1024)
MAX_DATABASE_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_DOCKER_BYTES = 32 * 1024 * 1024


class IsolationError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = str(code or "runtime_isolation_failed")
        self.detail = str(detail or "")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_id(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_regular(
    path: Path,
    *,
    max_bytes: int,
    mode: int | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise IsolationError("required_file_missing", str(path)) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > max_bytes
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
        or (uid is not None and metadata.st_uid != uid)
        or (gid is not None and metadata.st_gid != gid)
    ):
        raise IsolationError("required_file_metadata_invalid", str(path))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise IsolationError("required_file_size_invalid", str(path))
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _decode_env_value(raw: str, *, key: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise IsolationError("env_value_invalid", key) from exc
        if not isinstance(decoded, str):
            raise IsolationError("env_value_invalid", key)
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise IsolationError("env_value_invalid", key)
        return value[1:-1].replace("\\'", "'")
    comment = re.search(r"\s+#", value)
    if comment is not None:
        value = value[: comment.start()].rstrip()
    return value


def _parse_env(raw: bytes, *, path: Path) -> tuple[dict[str, str], list[str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IsolationError("env_utf8_invalid", str(path)) from exc
    values: dict[str, str] = {}
    keys_in_order: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_ASSIGNMENT_RE.fullmatch(raw_line)
        if match is None:
            raise IsolationError("env_assignment_invalid", f"{path}:{line_number}")
        key = match.group("key")
        if key in values:
            raise IsolationError("env_key_duplicate", f"{path}:{key}")
        value = _decode_env_value(match.group("value"), key=key)
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise IsolationError("env_value_invalid", key)
        values[key] = value
        keys_in_order.append(key)
    return values, keys_in_order


def _strict_env(
    path: Path,
    *,
    expected_keys: Sequence[str] | None = None,
) -> tuple[dict[str, str], bytes]:
    raw = _read_regular(path, max_bytes=MAX_ENV_BYTES, mode=0o600, uid=1000, gid=1000)
    values, keys = _parse_env(raw, path=path)
    if expected_keys is not None:
        if tuple(keys) != tuple(expected_keys) or set(values) != set(expected_keys):
            raise IsolationError("env_key_contract_invalid", str(path))
        if any(not values[key] for key in expected_keys):
            raise IsolationError("env_value_missing", str(path))
    return values, raw


def _atomic_temp_names(path: Path) -> tuple[str, ...]:
    prefix = f".{path.name}."
    try:
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise IsolationError("runtime_env_parent_invalid", str(path.parent)) from exc
    try:
        parent = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != 1000
            or parent.st_gid != 1000
            or stat.S_IMODE(parent.st_mode)
            not in {0o700, 0o750, 0o755, 0o770, 0o775}
        ):
            raise IsolationError("runtime_env_parent_invalid", str(path.parent))
        names = tuple(
            sorted(
                name
                for name in os.listdir(descriptor)
                if name.startswith(prefix) and name.endswith(".tmp")
            )
        )
    finally:
        os.close(descriptor)
    if len(names) > 32:
        raise IsolationError("runtime_env_atomic_temp_set_invalid", str(path))
    return names


def _reject_atomic_user_temps(path: Path) -> None:
    if _atomic_temp_names(path):
        raise IsolationError("runtime_env_atomic_temp_present", str(path))


def _cleanup_atomic_user_temps(path: Path) -> None:
    names = _atomic_temp_names(path)
    if not names:
        return
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        for name in names:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != 1000
                or metadata.st_gid != 1000
                or metadata.st_nlink != 1
            ):
                raise IsolationError("runtime_env_atomic_temp_invalid", name)
            os.unlink(name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_user_file(path: Path, encoded: bytes) -> None:
    try:
        parent = path.parent.lstat()
    except FileNotFoundError as exc:
        raise IsolationError("runtime_env_parent_missing", str(path.parent)) from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != 1000
        or parent.st_gid != 1000
        or stat.S_IMODE(parent.st_mode) not in {0o700, 0o750, 0o755, 0o770, 0o775}
    ):
        raise IsolationError("runtime_env_parent_invalid", str(path.parent))
    if os.geteuid() not in {0, 1000}:
        raise IsolationError("runtime_env_owner_authority_invalid")
    _cleanup_atomic_user_temps(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            if os.geteuid() == 0:
                os.fchown(descriptor, 1000, 1000)
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _encoded_env(values: Mapping[str, str], keys: Sequence[str]) -> bytes:
    return (
        "".join(
            f"{key}={json.dumps(values[key], ensure_ascii=True)}\n" for key in keys
        )
    ).encode("utf-8")


def _ensure_runtime_secret(path: Path, *, key: str) -> tuple[bytes, bool]:
    raw = _read_regular(
        path,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    values, keys = _parse_env(raw, path=path)
    current = values.get(key, "")
    if current:
        if len(current) < 43:
            raise IsolationError("runtime_secret_too_short", key)
        return raw, False
    generated = secrets.token_urlsafe(48)
    if len(generated) < 43:  # pragma: no cover - token_urlsafe contract
        raise IsolationError("runtime_secret_generation_failed", key)
    replacement = f"{key}={json.dumps(generated)}\n".encode("utf-8")
    if key not in values:
        encoded = raw + (b"" if raw.endswith(b"\n") else b"\n") + replacement
    else:
        rewritten: list[bytes] = []
        replaced = False
        for line in raw.splitlines(keepends=True):
            decoded = line.decode("utf-8").rstrip("\r\n")
            match = ENV_ASSIGNMENT_RE.fullmatch(decoded)
            if match is not None and match.group("key") == key:
                rewritten.append(replacement)
                replaced = True
            else:
                rewritten.append(line)
        if not replaced or keys.count(key) != 1:  # pragma: no cover - parser guard
            raise IsolationError("runtime_secret_rewrite_failed", key)
        encoded = b"".join(rewritten)
    _atomic_user_file(path, encoded)
    after = _read_regular(
        path,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    after_values, _after_keys = _parse_env(after, path=path)
    if after_values.get(key) != generated:
        raise IsolationError("runtime_secret_write_mismatch", key)
    return after, True


def _set_runtime_value(path: Path, *, key: str, value: str) -> tuple[bytes, bool]:
    raw = _read_regular(
        path,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    values, keys = _parse_env(raw, path=path)
    if values.get(key) == value:
        return raw, False
    replacement = f"{key}={json.dumps(value)}\n".encode("utf-8")
    if key not in values:
        encoded = raw + (b"" if raw.endswith(b"\n") else b"\n") + replacement
    else:
        rewritten: list[bytes] = []
        replaced = False
        for line in raw.splitlines(keepends=True):
            decoded = line.decode("utf-8").rstrip("\r\n")
            match = ENV_ASSIGNMENT_RE.fullmatch(decoded)
            if match is not None and match.group("key") == key:
                rewritten.append(replacement)
                replaced = True
            else:
                rewritten.append(line)
        if not replaced or keys.count(key) != 1:  # pragma: no cover - parser guard
            raise IsolationError("runtime_value_rewrite_failed", key)
        encoded = b"".join(rewritten)
    _atomic_user_file(path, encoded)
    after = _read_regular(
        path,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    after_values, _after_keys = _parse_env(after, path=path)
    if after_values.get(key) != value:
        raise IsolationError("runtime_value_write_mismatch", key)
    return after, True


def _mail_source_keys(values: Mapping[str, str]) -> tuple[str, ...]:
    present = tuple(key for key in MAIL_KEYS if key in values)
    if present not in ((), LEGACY_MAIL_KEYS, MAIL_KEYS):
        raise IsolationError("legacy_registration_email_partial")
    return present


def _validate_mail_source_against_dedicated(
    source: Mapping[str, str],
    source_keys: Sequence[str],
    dedicated: Mapping[str, str],
    *,
    error_code: str,
) -> None:
    if any(not source[key] for key in source_keys):
        raise IsolationError("registration_email_value_missing")
    if any(source[key] != dedicated[key] for key in source_keys):
        raise IsolationError(error_code)


def prepare_registration_email_input() -> dict[str, object]:
    for path in (ROOT_ENV, REGISTRATION_ENV, SCENE_ENV):
        _cleanup_atomic_user_temps(path)
    root_raw = _read_regular(
        ROOT_ENV,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    root_values, _root_keys = _parse_env(root_raw, path=ROOT_ENV)
    present = _mail_source_keys(root_values)
    dedicated_exists = REGISTRATION_ENV.exists()
    if not present and not dedicated_exists:
        raise IsolationError("registration_email_source_missing")
    if present:
        source = {key: root_values[key] for key in present}
        if dedicated_exists:
            dedicated, dedicated_raw = _strict_env(
                REGISTRATION_ENV,
                expected_keys=MAIL_KEYS,
            )
            _validate_mail_source_against_dedicated(
                source,
                present,
                dedicated,
                error_code="registration_email_input_conflict",
            )
        elif present == LEGACY_MAIL_KEYS:
            raise IsolationError("registration_email_source_missing")
        else:
            _atomic_user_file(REGISTRATION_ENV, _encoded_env(source, MAIL_KEYS))
            dedicated, dedicated_raw = _strict_env(
                REGISTRATION_ENV,
                expected_keys=MAIL_KEYS,
            )
            if dedicated != source:  # pragma: no cover - post-write guard
                raise IsolationError("registration_email_write_mismatch")
    else:
        _dedicated, dedicated_raw = _strict_env(
            REGISTRATION_ENV,
            expected_keys=MAIL_KEYS,
        )
    scene_raw, bridge_token_created = _ensure_runtime_secret(
        SCENE_ENV,
        key=RENDER_BRIDGE_TOKEN_KEY,
    )
    _root_after_cloud, cloudflared_image_changed = _set_runtime_value(
        ROOT_ENV,
        key=CLOUDFLARED_IMAGE_KEY,
        value=CLOUDFLARED_IMAGE,
    )
    _root_after_bind, api_host_bind_changed = _set_runtime_value(
        ROOT_ENV,
        key=API_HOST_BIND_KEY,
        value="127.0.0.1",
    )
    root_after, api_host_port_changed = _set_runtime_value(
        ROOT_ENV,
        key=API_HOST_PORT_KEY,
        value="8097",
    )
    return {
        "bridge_token_created": bridge_token_created,
        "api_host_bind_changed": api_host_bind_changed,
        "api_host_port_changed": api_host_port_changed,
        "cloudflared_image_changed": cloudflared_image_changed,
        "key_count": len(MAIL_KEYS),
        "legacy_source_present": bool(present),
        "registration_env": str(REGISTRATION_ENV),
        "registration_env_sha256": _sha256_id(dedicated_raw),
        "root_env_sha256": _sha256_id(root_after),
        "scene_env_sha256": _sha256_id(scene_raw),
        "status": "prepared",
    }


def _validate_runtime_inputs(*, require_legacy_mail: bool) -> dict[str, object]:
    for path in dict.fromkeys((ROOT_ENV, *RUNTIME_INPUTS)):
        _reject_atomic_user_temps(path)
    root_raw = _read_regular(
        ROOT_ENV,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    root_values, _root_keys = _parse_env(root_raw, path=ROOT_ENV)
    if (
        root_values.get(CLOUDFLARED_IMAGE_KEY) != CLOUDFLARED_IMAGE
        or root_values.get(API_HOST_BIND_KEY) != "127.0.0.1"
        or root_values.get(API_HOST_PORT_KEY) != "8097"
    ):
        raise IsolationError("root_runtime_binding_invalid")
    dedicated, registration_raw = _strict_env(
        REGISTRATION_ENV,
        expected_keys=MAIL_KEYS,
    )
    google, google_raw = _strict_env(GOOGLE_ENV, expected_keys=GOOGLE_KEYS)
    _ = google
    present = _mail_source_keys(root_values)
    if require_legacy_mail:
        if present not in (LEGACY_MAIL_KEYS, MAIL_KEYS):
            raise IsolationError("legacy_registration_email_source_invalid")
        _validate_mail_source_against_dedicated(
            root_values,
            present,
            dedicated,
            error_code="registration_email_source_mismatch",
        )
    elif present:
        raise IsolationError("legacy_registration_email_not_purged")
    digests: dict[str, str] = {
        str(ROOT_ENV): _sha256_id(root_raw),
        str(REGISTRATION_ENV): _sha256_id(registration_raw),
        str(GOOGLE_ENV): _sha256_id(google_raw),
    }
    for path in (SCENE_ENV, DATABASE_ENV, ADMISSION_ENV):
        _values, raw = _strict_env(path)
        digests[str(path)] = _sha256_id(raw)
    return {
        "file_digests": dict(sorted(digests.items())),
        "google_key_count": len(GOOGLE_KEYS),
        "legacy_registration_email_present": bool(present),
        "registration_email_key_count": len(MAIL_KEYS),
    }


def _validated_retirement_mount(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != RETIREMENT_MOUNT_KEYS:
        raise IsolationError("runtime_retirement_mount_invalid")
    result = {
        "destination": str(value.get("destination") or ""),
        "driver": str(value.get("driver") or ""),
        "mode": str(value.get("mode") or ""),
        "name": str(value.get("name") or ""),
        "propagation": str(value.get("propagation") or ""),
        "rw": value.get("rw"),
        "source": str(value.get("source") or ""),
        "type": str(value.get("type") or ""),
    }
    if (
        not isinstance(result["rw"], bool)
        or result["type"] not in {"bind", "tmpfs", "volume"}
        or not result["destination"].startswith("/")
        or not result["source"].startswith("/")
        and result["type"] != "volume"
        or result["type"] == "volume"
        and (not result["name"] or result["source"] != result["name"])
        or result["type"] != "volume"
        and result["name"]
        or any("\x00" in str(item) or "\n" in str(item) for item in result.values())
    ):
        raise IsolationError("runtime_retirement_mount_invalid")
    return result


def _validated_retirement_container(
    value: object,
    *,
    allow_desired: bool = False,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != RETIREMENT_CONTAINER_KEYS:
        raise IsolationError("runtime_retirement_container_invalid")
    name = str(value.get("name") or "")
    compose_project = str(value.get("compose_project") or "")
    compose_service = str(value.get("compose_service") or "")
    container_id = str(value.get("container_id") or "")
    created_at = str(value.get("created_at") or "")
    image = str(value.get("image") or "")
    image_id = str(value.get("image_id") or "")
    networks = value.get("networks")
    mounts = value.get("mounts")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name)
        or name in ALLOWED_PROPERTYQUARRY_CONTAINERS
        and not allow_desired
        or not allow_desired
        and not any(pattern.fullmatch(name) for pattern in RETIREMENT_LEGACY_NAME_PATTERNS)
        or not re.fullmatch(r"[0-9a-f]{64}", container_id)
        or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?",
            created_at,
        )
        or not image
        or len(image.encode("utf-8")) > 512
        or not SHA256_ID_RE.fullmatch(image_id)
        or not isinstance(networks, list)
        or not isinstance(mounts, list)
        or any(
            not isinstance(item, str)
            or not item
            or len(item.encode("utf-8")) > 255
            or "\x00" in item
            or "\n" in item
            for item in networks
        )
        or networks != sorted(set(networks))
        or len(compose_project.encode("utf-8")) > 255
        or len(compose_service.encode("utf-8")) > 255
        or "\x00" in compose_project
        or "\n" in compose_project
        or "\x00" in compose_service
        or "\n" in compose_service
    ):
        raise IsolationError("runtime_retirement_container_invalid", name)
    validated_mounts = [_validated_retirement_mount(item) for item in mounts]
    if validated_mounts != sorted(
        validated_mounts,
        key=lambda item: _canonical_json(item),
    ) or len({_canonical_json(item) for item in validated_mounts}) != len(
        validated_mounts
    ):
        raise IsolationError("runtime_retirement_container_invalid", name)
    return {
        "compose_project": compose_project,
        "compose_service": compose_service,
        "container_id": container_id,
        "created_at": created_at,
        "image": image,
        "image_id": image_id,
        "mounts": validated_mounts,
        "name": name,
        "networks": list(networks),
    }


def _validated_retirement_contract(
    value: object,
    *,
    runtime_sha: str,
    deployment_id: str,
) -> dict[str, object]:
    expected_keys = {
        "containers",
        "deployment_id",
        "desired_live_allowlist",
        "operation",
        "preserve_volumes",
        "receipt_path",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise IsolationError("runtime_retirement_contract_invalid")
    containers = value.get("containers")
    desired = value.get("desired_live_allowlist")
    expected_receipt = (
        RECEIPT_ROOT
        / runtime_sha
        / deployment_id
        / f"{RETIREMENT_OPERATION}.json"
    )
    if (
        value.get("operation") != RETIREMENT_OPERATION
        or value.get("deployment_id") != deployment_id
        or value.get("preserve_volumes") is not True
        or value.get("receipt_path") != str(expected_receipt)
        or not isinstance(containers, list)
        or not isinstance(desired, list)
        or desired != sorted(ALLOWED_PROPERTYQUARRY_CONTAINERS)
    ):
        raise IsolationError("runtime_retirement_contract_invalid")
    validated = [_validated_retirement_container(item) for item in containers]
    if validated != sorted(validated, key=lambda item: str(item["name"])):
        raise IsolationError("runtime_retirement_contract_invalid")
    names = [str(item["name"]) for item in validated]
    if len(names) != len(set(names)):
        raise IsolationError("runtime_retirement_contract_invalid")
    return {
        "containers": validated,
        "deployment_id": deployment_id,
        "desired_live_allowlist": list(desired),
        "operation": RETIREMENT_OPERATION,
        "preserve_volumes": True,
        "receipt_path": str(expected_receipt),
    }


def _contract(
    args: argparse.Namespace,
) -> tuple[
    backup_contract.BackupRequest,
    dict[str, object],
    dict[str, object],
    object,
    object,
    str,
]:
    runtime_sha = str(args.runtime_sha or "").strip()
    deployment_id = str(args.deployment_id or "").strip()
    envelope_sha = str(args.envelope_sha or "").strip()
    web_image = str(args.web_image or "").strip()
    render_image = str(args.render_image or "").strip()
    cloudflared_image = str(args.cloudflared_image or "").strip()
    database_image = str(args.database_image or "").strip()
    api_host_ip = str(args.api_host_ip or "").strip()
    try:
        api_host_port = int(args.api_host_port)
        api_container_port = int(args.api_container_port)
    except (TypeError, ValueError) as exc:
        raise IsolationError("api_port_invalid") from exc
    if not RUNTIME_SHA_RE.fullmatch(runtime_sha):
        raise IsolationError("runtime_sha_invalid")
    if not DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise IsolationError("deployment_id_invalid")
    if not SHA256_RE.fullmatch(envelope_sha):
        raise IsolationError("envelope_sha_invalid")
    if not IMAGE_RE.fullmatch(web_image) or not IMAGE_RE.fullmatch(render_image):
        raise IsolationError("runtime_image_invalid")
    if not IMAGE_RE.fullmatch(cloudflared_image):
        raise IsolationError("cloudflared_image_invalid")
    if not IMAGE_RE.fullmatch(database_image):
        raise IsolationError("database_image_invalid")
    if (
        api_host_ip != "127.0.0.1"
        or api_host_port != 8097
        or api_container_port != 8090
    ):
        raise IsolationError("api_port_contract_invalid")
    paths = backup_contract.BackupPaths()
    backup_receipt = (
        paths.receipt_root / runtime_sha / deployment_id / "create.json"
    )
    request = backup_contract.BackupRequest(
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        envelope_sha=envelope_sha,
        web_image=web_image,
        render_image=render_image,
        database_image=database_image,
        receipt_path=backup_receipt,
        encryption_key_path=backup_contract.EXPECTED_ENCRYPTION_KEY_PATH,
    )
    try:
        backup_contract._validate_request(request, paths)  # noqa: SLF001
        private, public, key_id = backup_contract._load_receipt_authority(  # noqa: SLF001
            paths,
            require_root_owner=True,
        )
        bindings = backup_contract._installed_bindings(  # noqa: SLF001
            paths,
            request,
            receipt_key_id=key_id,
            require_root_owner=True,
        )
        authority, _authority_raw, _authority_digest = backup_contract._load_json_file(  # noqa: SLF001
            paths.authority
        )
        plan, _plan_raw, _plan_digest = backup_contract._load_json_file(  # noqa: SLF001
            paths.transaction_plan
        )
        scene_raw = _read_regular(
            SCENE_ENV,
            max_bytes=MAX_ENV_BYTES,
            mode=0o600,
            uid=1000,
            gid=1000,
        )
        scene_fields = {
            "scene_video_env_path": str(SCENE_ENV),
            "scene_video_env_mode": 0o600,
            "scene_video_env_uid": 1000,
            "scene_video_env_gid": 1000,
            "scene_video_env_digest": _sha256_id(scene_raw),
        }
        runtime_fields = {
            "api_container_port": api_container_port,
            "api_host_ip": api_host_ip,
            "api_host_port": api_host_port,
            "cloudflared_image": cloudflared_image,
            "database_image": database_image,
            **scene_fields,
        }
        if (
            any(authority.get(key) != value for key, value in runtime_fields.items())
            or any(plan.get(key) != value for key, value in runtime_fields.items())
        ):
            raise IsolationError("runtime_isolation_input_binding_invalid")
        if authority.get("runtime_retirement") != plan.get("runtime_retirement"):
            raise IsolationError("runtime_retirement_contract_mismatch")
        runtime_retirement = _validated_retirement_contract(
            authority.get("runtime_retirement"),
            runtime_sha=runtime_sha,
            deployment_id=deployment_id,
        )
        retirement_digest = _sha256_id(_canonical_json(runtime_retirement))
        if bindings.get("runtime_retirement_digest") != retirement_digest:
            raise IsolationError("runtime_retirement_digest_invalid")
        bindings.update(runtime_fields)
    except backup_contract.BackupError as exc:
        raise IsolationError("installed_release_contract_invalid", exc.code) from exc
    return request, bindings, runtime_retirement, private, public, key_id


def _receipt_binding_fields(
    bindings: Mapping[str, object],
) -> dict[str, object]:
    if any(key not in bindings for key in ISOLATION_COMMON_BINDING_KEYS):
        raise IsolationError("runtime_receipt_binding_missing")
    return {key: bindings[key] for key in ISOLATION_COMMON_BINDING_KEYS}


def verify_isolation_inputs(args: argparse.Namespace) -> dict[str, object]:
    _request, bindings, _retirement, _private, _public, _key_id = _contract(args)
    current_runtime_inputs = _current_runtime_inputs()
    if current_runtime_inputs != bindings.get("pre_purge_runtime_inputs"):
        raise IsolationError("pre_purge_runtime_inputs_invalid")
    inputs = _validate_runtime_inputs(require_legacy_mail=True)
    expected_root_digest = str(args.pre_purge_root_env_digest or "").strip()
    if (
        not SHA256_ID_RE.fullmatch(expected_root_digest)
        or inputs["file_digests"].get(str(ROOT_ENV)) != expected_root_digest
    ):
        raise IsolationError("pre_purge_root_env_digest_invalid")
    return {
        "bindings": bindings,
        "inputs": inputs,
        "mutation_performed": False,
        "status": "verified",
    }


def _current_runtime_inputs() -> list[dict[str, object]]:
    try:
        descriptors, _contents = backup_contract._runtime_input_snapshot()  # noqa: SLF001
    except backup_contract.BackupError as exc:
        raise IsolationError("runtime_input_snapshot_invalid", exc.code) from exc
    return descriptors


def _read_backup_receipt(
    request: backup_contract.BackupRequest,
    *,
    public_key: object,
    key_id: str,
    bindings: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    expected_path = (
        backup_contract.RECEIPT_ROOT
        / request.runtime_sha
        / request.deployment_id
        / "create.json"
    )
    try:
        raw = backup_contract._read_regular_nofollow(  # noqa: SLF001
            expected_path,
            max_bytes=16 * 1024 * 1024,
        )
        wrapper = json.loads(raw)
        payload = backup_contract._verify_receipt_wrapper(  # noqa: SLF001
            wrapper,
            public_key,
            key_id,
        )
        final_path = (
            backup_contract.REMOTE_ROOT
            / request.runtime_sha
            / request.deployment_id
        )
        database_identity = backup_contract._validated_database_substrate(  # noqa: SLF001
            payload.get("database_substrate_before")
        )
        if (
            payload.get("database_substrate_after") != database_identity
            or database_identity != bindings.get("database_substrate")
            or payload.get("database_image_id") != database_identity["image_id"]
            or payload.get("database_repo_digest")
            != database_identity["repo_digest"]
        ):
            raise IsolationError("backup_database_identity_invalid")
        backup_started = _exact_int(
            payload.get("started_at_epoch"),
            1,
            (1 << 62) - 1,
        )
        backup_finished = _exact_int(
            payload.get("finished_at_epoch"),
            1,
            (1 << 62) - 1,
        )
        transaction_started = _exact_int(
            bindings.get("transaction_started_at_epoch"),
            1,
            (1 << 62) - 1,
        )
        max_age = _exact_int(
            bindings.get("backup_max_age_seconds"),
            1,
            3600,
        )
        if (
            backup_finished < backup_started
            or backup_started < transaction_started
            or backup_finished > int(time.time()) + 30
            or backup_finished - backup_started > max_age
            or int(time.time()) - backup_finished > max_age
        ):
            raise IsolationError("backup_receipt_time_invalid")
        expected_binding_keys = (
            "authority_digest",
            "authority_signature_digest",
            "backup_max_age_seconds",
            "config_digest",
            "package_authority_key_id",
            "package_manifest_digest",
            "package_manifest_signature_digest",
            "plan_digest",
            "transaction_started_at_epoch",
        )
        expected_bindings = {
            key: bindings[key] for key in expected_binding_keys
        }
        expected_bindings.update(
            {
                "database_image": request.database_image,
                "database_image_id": database_identity["image_id"],
                "database_repo_digest": database_identity["repo_digest"],
                "database_substrate_after": database_identity,
                "database_substrate_before": database_identity,
                "deployment_id": request.deployment_id,
                "pre_purge_runtime_inputs": bindings[
                    "pre_purge_runtime_inputs"
                ],
            }
        )
        if not backup_contract._receipt_payload_matches(  # noqa: SLF001
            payload,
            request,
            final_path,
            database_identity,
            expected_bindings,
        ):
            raise IsolationError("backup_receipt_binding_invalid")
        specs = backup_contract.production_artifact_specs()
        backup_contract._validate_signed_receipt_remote(  # noqa: SLF001
            payload,
            final_path,
            specs,
        )
    except IsolationError:
        raise
    except Exception as exc:
        raise IsolationError("backup_receipt_invalid") from exc
    runtime_artifacts = [
        artifact
        for artifact in payload.get("artifacts", [])
        if isinstance(artifact, dict)
        and artifact.get("name") == "runtime-identity-config"
    ]
    expected_coverage = [str(path) for path in RUNTIME_INPUTS]
    if (
        len(runtime_artifacts) != 1
        or runtime_artifacts[0].get("coverage") != expected_coverage
        or not isinstance(runtime_artifacts[0].get("verification"), dict)
        or runtime_artifacts[0]["verification"].get("runtime_inputs")
        != bindings.get("pre_purge_runtime_inputs")
    ):
        raise IsolationError("backup_runtime_input_coverage_invalid")
    return dict(payload), _sha256_id(raw)


def _rollback_parent(runtime_sha: str, deployment_id: str) -> Path:
    try:
        root_metadata = ROLLBACK_ROOT.lstat()
    except FileNotFoundError as exc:
        raise IsolationError("rollback_root_missing", str(ROLLBACK_ROOT)) from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or root_metadata.st_uid != 0
        or root_metadata.st_gid != 0
    ):
        raise IsolationError("rollback_root_invalid", str(ROLLBACK_ROOT))
    parent = ROLLBACK_ROOT
    for component in (runtime_sha, deployment_id):
        child = parent / component
        created = False
        try:
            child.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        metadata = child.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != 0
            or metadata.st_gid != 0
        ):
            raise IsolationError("rollback_parent_invalid", str(child))
        if created:
            _fsync_directory(parent)
        parent = child
    return parent


def _rollback_master_key() -> tuple[bytes, str]:
    encoded = _read_regular(
        backup_contract.EXPECTED_ENCRYPTION_KEY_PATH,
        max_bytes=65,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    if re.fullmatch(rb"[0-9a-f]{64}\n", encoded) is None:
        raise IsolationError("rollback_encryption_key_invalid")
    key = bytes.fromhex(encoded.decode("ascii").strip())
    return key, _sha256_id(key)


def _rollback_artifact_path(runtime_sha: str, deployment_id: str) -> Path:
    return (
        ROLLBACK_ROOT
        / runtime_sha
        / deployment_id
        / "root-env.pre-purge.enc"
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_rollback_artifact_from(
    path: Path,
    *,
    runtime_sha: str,
    deployment_id: str,
    expected_pre_purge_digest: str,
) -> tuple[bytes, dict[str, object]]:
    ciphertext = _read_regular(
        path,
        max_bytes=MAX_ROLLBACK_BYTES,
        mode=0o600,
        uid=0,
        gid=0,
    )
    master_key, key_id = _rollback_master_key()
    plaintext = bytearray()

    def receive(chunk: bytes) -> None:
        if len(plaintext) + len(chunk) > MAX_ENV_BYTES:
            raise IsolationError("rollback_plaintext_oversized")
        plaintext.extend(chunk)

    try:
        metadata = backup_contract.decrypt_stream(
            path,
            receive,
            master_key=master_key,
            expected_runtime_sha=runtime_sha,
            expected_deployment_id=deployment_id,
            expected_artifact_name=ROLLBACK_ARTIFACT_NAME,
            expected_artifact_kind=ROLLBACK_ARTIFACT_KIND,
        )
    except IsolationError:
        raise
    except Exception as exc:
        raise IsolationError("rollback_artifact_invalid") from exc
    encoded = bytes(plaintext)
    if (
        not encoded
        or _sha256_id(encoded) != expected_pre_purge_digest
        or metadata.get("ciphertext_sha256") != hashlib.sha256(ciphertext).hexdigest()
        or metadata.get("ciphertext_bytes") != len(ciphertext)
        or metadata.get("plaintext_sha256") != hashlib.sha256(encoded).hexdigest()
        or metadata.get("plaintext_bytes") != len(encoded)
    ):
        raise IsolationError("rollback_artifact_binding_invalid")
    values, _keys = _parse_env(encoded, path=ROOT_ENV)
    present = tuple(key for key in MAIL_KEYS if key in values)
    if present not in (LEGACY_MAIL_KEYS, MAIL_KEYS):
        raise IsolationError("rollback_artifact_legacy_source_invalid")
    evidence = {
        "ciphertext_bytes": len(ciphertext),
        "ciphertext_sha256": _sha256_id(ciphertext),
        "encryption_key_id": key_id,
        "path": str(path),
        "plaintext_bytes": len(encoded),
        "plaintext_sha256": expected_pre_purge_digest,
    }
    return encoded, evidence


def _load_rollback_artifact(
    *,
    runtime_sha: str,
    deployment_id: str,
    expected_pre_purge_digest: str,
) -> tuple[bytes, dict[str, object]]:
    return _load_rollback_artifact_from(
        _rollback_artifact_path(runtime_sha, deployment_id),
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        expected_pre_purge_digest=expected_pre_purge_digest,
    )


def _ensure_rollback_artifact(
    *,
    runtime_sha: str,
    deployment_id: str,
    preimage: bytes,
    expected_pre_purge_digest: str,
) -> tuple[bytes, dict[str, object]]:
    if _sha256_id(preimage) != expected_pre_purge_digest:
        raise IsolationError("pre_purge_root_env_digest_invalid")
    parent = _rollback_parent(runtime_sha, deployment_id)
    path = _rollback_artifact_path(runtime_sha, deployment_id)
    pending = parent / "root-env.pre-purge.pending.enc"
    if path.exists() and pending.exists():
        final_metadata = path.lstat()
        pending_metadata = pending.lstat()
        if (
            not stat.S_ISREG(final_metadata.st_mode)
            or stat.S_ISLNK(final_metadata.st_mode)
            or final_metadata.st_dev != pending_metadata.st_dev
            or final_metadata.st_ino != pending_metadata.st_ino
            or final_metadata.st_nlink != 2
            or pending_metadata.st_nlink != 2
        ):
            raise IsolationError("rollback_artifact_pending_conflict")
        pending.unlink()
        _fsync_directory(parent)
    if not path.exists() and pending.exists():
        try:
            pending_preimage, _pending_evidence = _load_rollback_artifact_from(
                pending,
                runtime_sha=runtime_sha,
                deployment_id=deployment_id,
                expected_pre_purge_digest=expected_pre_purge_digest,
            )
        except IsolationError:
            pending.unlink()
            _fsync_directory(parent)
        else:
            if pending_preimage != preimage:
                raise IsolationError("rollback_artifact_pending_conflict")
            os.link(pending, path, follow_symlinks=False)
            pending.unlink()
            _fsync_directory(parent)
    if not path.exists():
        master_key, _key_id = _rollback_master_key()
        try:
            backup_contract.encrypt_stream(
                io.BytesIO(preimage),
                pending,
                master_key=master_key,
                runtime_sha=runtime_sha,
                deployment_id=deployment_id,
                artifact_name=ROLLBACK_ARTIFACT_NAME,
                artifact_kind=ROLLBACK_ARTIFACT_KIND,
            )
            os.chmod(pending, 0o600, follow_symlinks=False)
            os.chown(pending, 0, 0, follow_symlinks=False)
            _fsync_directory(parent)
            pending_preimage, _pending_evidence = _load_rollback_artifact_from(
                pending,
                runtime_sha=runtime_sha,
                deployment_id=deployment_id,
                expected_pre_purge_digest=expected_pre_purge_digest,
            )
            if pending_preimage != preimage:
                raise IsolationError("rollback_artifact_write_mismatch")
            os.link(pending, path, follow_symlinks=False)
            pending.unlink()
            _fsync_directory(parent)
        except backup_contract.BackupError as exc:
            raise IsolationError("rollback_artifact_write_failed", exc.code) from exc
    return _load_rollback_artifact(
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        expected_pre_purge_digest=expected_pre_purge_digest,
    )


def _filtered_root_env(raw: bytes) -> tuple[bytes, int]:
    values, _keys = _parse_env(raw, path=ROOT_ENV)
    present = _mail_source_keys(values)
    if not present:
        return raw, 0
    kept: list[bytes] = []
    removed: list[str] = []
    for line in raw.splitlines(keepends=True):
        try:
            decoded = line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:  # pragma: no cover - parsed above
            raise IsolationError("env_utf8_invalid", str(ROOT_ENV)) from exc
        match = ENV_ASSIGNMENT_RE.fullmatch(decoded)
        if match is not None and match.group("key") in MAIL_KEYS:
            removed.append(match.group("key"))
            continue
        kept.append(line)
    if tuple(removed) != tuple(key for key in _keys if key in MAIL_KEYS):
        raise IsolationError("legacy_registration_email_purge_invalid")
    encoded = b"".join(kept)
    if not encoded:
        raise IsolationError("root_env_would_be_empty")
    return encoded, len(removed)


def _purged_root_env() -> tuple[int, str]:
    raw = _read_regular(
        ROOT_ENV,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    encoded, removed = _filtered_root_env(raw)
    if not removed:
        return 0, _sha256_id(raw)
    _atomic_user_file(ROOT_ENV, encoded)
    after = _read_regular(
        ROOT_ENV,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    after_values, _after_keys = _parse_env(after, path=ROOT_ENV)
    if any(key in after_values for key in MAIL_KEYS):
        raise IsolationError("legacy_registration_email_purge_incomplete")
    return removed, _sha256_id(after)


def _docker(*argv: str, timeout: int = 60) -> str:
    try:
        completed = subprocess.run(
            (DOCKER_BIN, *argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "TZ": "UTC",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError("docker_unavailable") from exc
    if completed.returncode != 0:
        raise IsolationError("docker_command_failed", argv[0] if argv else "")
    if len(completed.stdout.encode("utf-8")) > MAX_DOCKER_BYTES:
        raise IsolationError("docker_output_oversized")
    return completed.stdout


def _all_containers() -> dict[str, dict[str, object]]:
    names = tuple(
        line.strip()
        for line in _docker("ps", "-a", "--format", "{{.Names}}").splitlines()
        if line.strip()
    )
    if len(names) != len(set(names)) or len(names) > 512:
        raise IsolationError("docker_container_set_invalid")
    containers: dict[str, dict[str, object]] = {}
    for name in names:
        try:
            loaded = json.loads(_docker("inspect", name))
        except (TypeError, ValueError) as exc:
            raise IsolationError("docker_inspect_invalid", name) from exc
        if not isinstance(loaded, list) or len(loaded) != 1 or not isinstance(loaded[0], dict):
            raise IsolationError("docker_inspect_invalid", name)
        containers[name] = loaded[0]
    return containers


def _retirement_mounts(
    container: Mapping[str, object],
    *,
    name: str,
) -> list[dict[str, object]]:
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        raise IsolationError("container_mounts_invalid", name)
    result: list[dict[str, object]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            raise IsolationError("container_mounts_invalid", name)
        mount_type = str(mount.get("Type") or "")
        mount_name = str(mount.get("Name") or "")
        source = str(mount.get("Source") or "")
        if mount_type == "volume":
            source = mount_name
        item = _validated_retirement_mount(
            {
                "destination": str(mount.get("Destination") or ""),
                "driver": str(mount.get("Driver") or ""),
                "mode": str(mount.get("Mode") or ""),
                "name": mount_name,
                "propagation": str(mount.get("Propagation") or ""),
                "rw": mount.get("RW"),
                "source": source,
                "type": mount_type,
            }
        )
        result.append(item)
    result.sort(key=lambda item: _canonical_json(item))
    if len({_canonical_json(item) for item in result}) != len(result):
        raise IsolationError("container_mount_duplicate", name)
    return result


def _retirement_observation(
    name: str,
    container: Mapping[str, object],
) -> dict[str, object]:
    config = container.get("Config")
    if not isinstance(config, dict):
        raise IsolationError("container_contract_invalid", name)
    labels = config.get("Labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in labels.items()
    ):
        raise IsolationError("container_labels_invalid", name)
    return _validated_retirement_container(
        {
            "compose_project": str(
                labels.get("com.docker.compose.project") or ""
            ),
            "compose_service": str(
                labels.get("com.docker.compose.service") or ""
            ),
            "container_id": str(container.get("Id") or ""),
            "created_at": str(container.get("Created") or ""),
            "image": str(config.get("Image") or ""),
            "image_id": str(container.get("Image") or ""),
            "mounts": _retirement_mounts(container, name=name),
            "name": name,
            "networks": sorted(_container_networks(container, name=name)),
        },
        allow_desired=name in ALLOWED_PROPERTYQUARRY_CONTAINERS,
    )


def _is_propertyquarry_container_match(
    name: str,
    container: Mapping[str, object],
) -> bool:
    config = container.get("Config")
    if not isinstance(config, dict):
        raise IsolationError("container_contract_invalid", name)
    labels = config.get("Labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        raise IsolationError("container_labels_invalid", name)
    compose_project = str(labels.get("com.docker.compose.project") or "")
    compose_service = str(labels.get("com.docker.compose.service") or "")
    return (
        name.startswith("propertyquarry-")
        or name.startswith("pq-")
        or compose_project.startswith("propertyquarry")
        or (
            compose_project == "property"
            and compose_service.startswith("propertyquarry-")
        )
    )


def _volume_observation(name: str) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", name):
        raise IsolationError("docker_volume_identity_invalid", name)
    try:
        loaded = json.loads(_docker("volume", "inspect", name))
    except (TypeError, ValueError) as exc:
        raise IsolationError("docker_volume_inspect_invalid", name) from exc
    if (
        not isinstance(loaded, list)
        or len(loaded) != 1
        or not isinstance(loaded[0], dict)
    ):
        raise IsolationError("docker_volume_inspect_invalid", name)
    volume = loaded[0]
    labels = volume.get("Labels")
    options = volume.get("Options")
    if labels is None:
        labels = {}
    if options is None:
        options = {}
    if (
        volume.get("Name") != name
        or not isinstance(labels, dict)
        or not isinstance(options, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for mapping in (labels, options)
            for key, value in mapping.items()
        )
    ):
        raise IsolationError("docker_volume_identity_invalid", name)
    result = {
        "created_at": str(volume.get("CreatedAt") or ""),
        "driver": str(volume.get("Driver") or ""),
        "labels": dict(sorted(labels.items())),
        "mountpoint": str(volume.get("Mountpoint") or ""),
        "name": name,
        "options": dict(sorted(options.items())),
        "scope": str(volume.get("Scope") or ""),
    }
    if (
        not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+ -]+Z?",
            str(result["created_at"]),
        )
        or not result["driver"]
        or not str(result["mountpoint"]).startswith("/")
        or not result["scope"]
        or any(
            "\x00" in str(item) or "\n" in str(item)
            for item in (
                result["created_at"],
                result["driver"],
                result["mountpoint"],
                result["scope"],
            )
        )
    ):
        raise IsolationError("docker_volume_identity_invalid", name)
    return result


def _retire_stale_runtime(
    runtime_retirement: Mapping[str, object],
    *,
    backup_receipt_sha256: str,
) -> dict[str, object]:
    if not SHA256_ID_RE.fullmatch(backup_receipt_sha256):
        raise IsolationError("backup_receipt_digest_invalid")
    signed_containers = runtime_retirement.get("containers")
    desired_live = runtime_retirement.get("desired_live_allowlist")
    if not isinstance(signed_containers, list) or not isinstance(desired_live, list):
        raise IsolationError("runtime_retirement_contract_invalid")
    signed_by_name = {
        str(item["name"]): item
        for item in signed_containers
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if len(signed_by_name) != len(signed_containers):
        raise IsolationError("runtime_retirement_contract_invalid")
    before = _all_containers()
    matching_names = {
        name
        for name, container in before.items()
        if _is_propertyquarry_container_match(name, container)
    }
    desired_names = set(desired_live)
    target_names = set(signed_by_name)
    unknown = sorted(matching_names - desired_names - target_names)
    missing = sorted(target_names - matching_names)
    if unknown:
        raise IsolationError("runtime_retirement_unknown_match", unknown[0])
    if missing:
        raise IsolationError("runtime_retirement_target_missing", missing[0])
    observed_targets = [
        _retirement_observation(name, before[name]) for name in sorted(target_names)
    ]
    if observed_targets != signed_containers:
        raise IsolationError("runtime_retirement_identity_mismatch")
    desired_before = {
        name: _retirement_observation(name, before[name])
        for name in sorted(matching_names & desired_names)
    }
    volume_names = sorted(
        {
            str(mount["name"])
            for container in observed_targets
            for mount in container["mounts"]
            if isinstance(mount, dict)
            and mount.get("type") == "volume"
            and mount.get("name")
        }
    )
    preserved_before = [_volume_observation(name) for name in volume_names]
    for name in sorted(target_names):
        current = _all_containers()
        if name not in current or _retirement_observation(
            name,
            current[name],
        ) != signed_by_name[name]:
            raise IsolationError("runtime_retirement_identity_mismatch", name)
        container_id = str(signed_by_name[name]["container_id"])
        output = tuple(
            line.strip()
            for line in _docker(
                "rm",
                "--force",
                container_id,
                timeout=120,
            ).splitlines()
            if line.strip()
        )
        if output != (container_id,):
            raise IsolationError("runtime_retirement_removal_invalid", name)
    after = _all_containers()
    retired_ids = {
        str(item["container_id"])
        for item in signed_containers
        if isinstance(item, dict)
    }
    if any(
        name in after or str(container.get("Id") or "") in retired_ids
        for name, container in after.items()
        if name in target_names or str(container.get("Id") or "") in retired_ids
    ):
        raise IsolationError("runtime_retirement_incomplete")
    after_matching = {
        name
        for name, container in after.items()
        if _is_propertyquarry_container_match(name, container)
    }
    after_unknown = sorted(after_matching - desired_names)
    if after_unknown:
        raise IsolationError("runtime_retirement_unknown_match", after_unknown[0])
    desired_after = {
        name: _retirement_observation(name, after[name])
        for name in sorted(after_matching & desired_names)
    }
    if desired_after != desired_before:
        raise IsolationError("runtime_retirement_desired_identity_changed")
    preserved_after = [_volume_observation(name) for name in volume_names]
    if preserved_after != preserved_before:
        raise IsolationError("runtime_retirement_volume_identity_changed")
    return {
        "backup_receipt_sha256": backup_receipt_sha256,
        "retired_containers": observed_targets,
        "preserved_volumes": preserved_after,
        "unknown_matches": [],
        "volumes_removed": False,
    }


def _container_env(container: Mapping[str, object], *, name: str) -> dict[str, str]:
    config = container.get("Config")
    if not isinstance(config, dict) or not isinstance(config.get("Env"), list):
        raise IsolationError("container_environment_invalid", name)
    values: dict[str, str] = {}
    for assignment in config["Env"]:
        if not isinstance(assignment, str) or "=" not in assignment:
            raise IsolationError("container_environment_invalid", name)
        key, value = assignment.split("=", 1)
        if key in values:
            raise IsolationError("container_environment_duplicate", f"{name}:{key}")
        values[key] = value
    return values


def _container_networks(container: Mapping[str, object], *, name: str) -> set[str]:
    network_settings = container.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        raise IsolationError("container_networks_invalid", name)
    networks = network_settings.get("Networks")
    if not isinstance(networks, dict):
        raise IsolationError("container_networks_invalid", name)
    return {str(network) for network in networks}


def _container_volumes(container: Mapping[str, object], *, name: str) -> set[str]:
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        raise IsolationError("container_mounts_invalid", name)
    observed: set[str] = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            raise IsolationError("container_mounts_invalid", name)
        if mount.get("Type") == "volume":
            observed.add(str(mount.get("Name") or ""))
        elif mount.get("Type") == "bind":
            observed.add(str(mount.get("Source") or ""))
    return observed


def _container_mount_contract(
    container: Mapping[str, object],
    *,
    name: str,
) -> set[tuple[str, str, str, bool]]:
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        raise IsolationError("container_mounts_invalid", name)
    observed: set[tuple[str, str, str, bool]] = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            raise IsolationError("container_mounts_invalid", name)
        mount_type = str(mount.get("Type") or "")
        if mount_type == "volume":
            source = str(mount.get("Name") or "")
            expected_source = f"/var/lib/docker/volumes/{source}/_data"
            if (
                mount.get("Driver") != "local"
                or mount.get("Source") != expected_source
                or mount.get("Propagation") != ""
            ):
                raise IsolationError("container_mount_identity_invalid", name)
        elif mount_type == "bind":
            source = str(mount.get("Source") or "")
            if (
                mount.get("Name") not in (None, "")
                or mount.get("Driver") not in (None, "")
                or mount.get("Propagation") != "rprivate"
            ):
                raise IsolationError("container_mount_identity_invalid", name)
        else:
            raise IsolationError("container_mount_type_invalid", name)
        item = (
            mount_type,
            source,
            str(mount.get("Destination") or ""),
            bool(mount.get("RW")),
        )
        if mount.get("Mode") != ("rw" if item[3] else "ro"):
            raise IsolationError("container_mount_identity_invalid", name)
        if item in observed:
            raise IsolationError("container_mount_duplicate", name)
        observed.add(item)
    return observed


def _canonical_repo_digest(reference: str) -> str:
    repository, digest = reference.rsplit("@", 1)
    prefix, separator, leaf = repository.rpartition("/")
    if ":" in leaf:
        leaf = leaf.split(":", 1)[0]
    canonical_repository = f"{prefix}{separator}{leaf}" if separator else leaf
    return f"{canonical_repository}@{digest}"


def _image_identities(references: Sequence[str]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for reference in sorted(set(references)):
        try:
            loaded = json.loads(_docker("image", "inspect", reference))
        except (TypeError, ValueError) as exc:
            raise IsolationError("docker_image_inspect_invalid", reference) from exc
        if not isinstance(loaded, list) or len(loaded) != 1 or not isinstance(loaded[0], dict):
            raise IsolationError("docker_image_inspect_invalid", reference)
        image = loaded[0]
        image_id = str(image.get("Id") or "")
        repo_digests = image.get("RepoDigests")
        if (
            not SHA256_ID_RE.fullmatch(image_id)
            or not isinstance(repo_digests, list)
            or _canonical_repo_digest(reference) not in repo_digests
        ):
            raise IsolationError("docker_image_identity_invalid", reference)
        identities[reference] = image_id
    return identities


def _property_volume_mountpoints() -> dict[str, str]:
    mountpoints: dict[str, str] = {}
    for name in sorted(PROPERTY_VOLUMES):
        try:
            loaded = json.loads(_docker("volume", "inspect", name))
        except (TypeError, ValueError) as exc:
            raise IsolationError("docker_volume_inspect_invalid", name) from exc
        if (
            not isinstance(loaded, list)
            or len(loaded) != 1
            or not isinstance(loaded[0], dict)
        ):
            raise IsolationError("docker_volume_inspect_invalid", name)
        volume = loaded[0]
        mountpoint = str(volume.get("Mountpoint") or "")
        expected_mountpoint = f"/var/lib/docker/volumes/{name}/_data"
        if (
            volume.get("Name") != name
            or volume.get("Driver") != "local"
            or volume.get("Scope") != "local"
            or volume.get("Options") not in (None, {})
            or mountpoint != expected_mountpoint
        ):
            raise IsolationError("docker_volume_identity_invalid", name)
        mountpoints[name] = mountpoint
    return mountpoints


def _expected_image(
    image_kind: str,
    *,
    request: backup_contract.BackupRequest,
    cloudflared_image: str,
    database_image: str,
) -> str:
    if image_kind == "web":
        return request.web_image
    if image_kind == "render":
        return request.render_image
    if image_kind == "database":
        return database_image
    if image_kind == "ingress":
        return cloudflared_image
    raise IsolationError("container_image_kind_invalid", image_kind)


def _verify_process_contract(
    container: Mapping[str, object],
    *,
    name: str,
    api_host_ip: str,
    api_host_port: int,
    api_container_port: int,
) -> None:
    config = container.get("Config")
    host = container.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise IsolationError("container_process_contract_invalid", name)
    expected_process = EXPECTED_PROCESS_CONTRACT[name]
    if (
        config.get("Cmd") != expected_process["cmd"]
        or config.get("Entrypoint") != expected_process["entrypoint"]
        or config.get("User") != expected_process["user"]
        or config.get("Healthcheck") != expected_process["healthcheck"]
    ):
        raise IsolationError("container_process_contract_invalid", name)
    (
        read_only,
        cap_drop,
        security_opt,
        pids_limit,
        restart_name,
        restart_count,
        cgroup_parent,
    ) = EXPECTED_HOST_CONTRACT[name]
    restart = host.get("RestartPolicy")
    expected_ports: dict[str, object] = {}
    if name == "propertyquarry-api-live":
        expected_ports = {
            f"{api_container_port}/tcp": [
                {"HostIp": api_host_ip, "HostPort": str(api_host_port)}
            ]
        }
    if (
        host.get("Privileged") is not False
        or host.get("ReadonlyRootfs") is not read_only
        or host.get("CapAdd") is not None
        or host.get("CapDrop") != cap_drop
        or host.get("SecurityOpt") != security_opt
        or host.get("PidsLimit") != pids_limit
        or host.get("PortBindings") != expected_ports
        or host.get("PublishAllPorts") is not False
        or host.get("Init") is not True
        or host.get("CgroupParent") != cgroup_parent
        or host.get("NetworkMode") != EXPECTED_NETWORK_MODES[name]
        or host.get("PidMode") != ""
        or host.get("IpcMode") != "private"
        or host.get("UTSMode") != ""
        or host.get("UsernsMode") != ""
        or host.get("CgroupnsMode") != "private"
        or host.get("Runtime") != "runc"
        or host.get("Devices") is not None
        or host.get("DeviceRequests") is not None
        or host.get("DeviceCgroupRules") is not None
        or host.get("VolumesFrom") is not None
        or not isinstance(restart, dict)
        or restart != {
            "MaximumRetryCount": restart_count,
            "Name": restart_name,
        }
    ):
        raise IsolationError("container_host_security_contract_invalid", name)
    network_settings = container.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        raise IsolationError("container_network_ports_invalid", name)
    network_ports = network_settings.get("Ports")
    if not isinstance(network_ports, dict):
        raise IsolationError("container_network_ports_invalid", name)
    published_ports: dict[str, object] = {}
    for port, bindings in network_ports.items():
        if not isinstance(port, str):
            raise IsolationError("container_network_ports_invalid", name)
        if bindings is None:
            continue
        if not isinstance(bindings, list):
            raise IsolationError("container_network_ports_invalid", name)
        published_ports[port] = bindings
    if published_ports != expected_ports:
        raise IsolationError("container_network_ports_invalid", name)


def _paths_overlap(first: str, second: Path) -> bool:
    candidate = Path(first)
    if not candidate.is_absolute():
        raise IsolationError("mount_path_invalid", first)
    try:
        candidate = candidate.resolve(strict=False)
        protected = second.resolve(strict=False)
    except OSError as exc:
        raise IsolationError("mount_path_resolution_failed", first) from exc
    return (
        candidate == protected
        or candidate in protected.parents
        or protected in candidate.parents
    )


def _protected_bind_overlap(mount: Mapping[str, object]) -> bool:
    if mount.get("Type") != "bind":
        return False
    protected_paths = {
        PROPERTY_ROOT,
        ROOT_ENV,
        SCENE_ENV,
        DATABASE_ENV,
        ADMISSION_ENV,
        GOOGLE_ENV,
        REGISTRATION_ENV,
    }
    source = str(mount.get("Source") or "")
    destination = str(mount.get("Destination") or "")
    return any(
        _paths_overlap(source, path) or _paths_overlap(destination, path)
        for path in protected_paths
    )


def _propertyquarry_key_digest(environment: Mapping[str, str]) -> str:
    encoded = b"".join(
        key.encode("utf-8") + b"\n"
        for key in sorted(
            key for key in environment if key.startswith("PROPERTYQUARRY_")
        )
    )
    return _sha256_id(encoded)


def _sensitive_scene_values() -> tuple[dict[str, str], str]:
    values, raw = _strict_env(SCENE_ENV)
    selected = dict(values)
    if (
        not selected
        or not selected.get(RENDER_BRIDGE_TOKEN_KEY)
        or not any(
            value
            for key, value in selected.items()
            if key != RENDER_BRIDGE_TOKEN_KEY
        )
    ):
        raise IsolationError("render_provider_input_missing")
    return selected, _sha256_id(raw)


def _protected_secret_values(
    registration: Mapping[str, str],
    google: Mapping[str, str],
    scene: Mapping[str, str],
) -> set[str]:
    scene_secret_names = {
        key
        for key in scene
        if key == RENDER_BRIDGE_TOKEN_KEY
        or re.search(
            r"(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE_KEY)(?:_|$)",
            key,
        )
        or key.endswith("_ACCOUNTS_JSON")
        or key == "DATABASE_URL"
        or key == "EA_TELEGRAM_BOT_REGISTRY_JSON"
    }
    return {
        value
        for value in (
            *(registration.get(key, "") for key in MAIL_SECRET_KEYS),
            *(google.get(key, "") for key in GOOGLE_SECRET_KEYS),
            *(scene.get(key, "") for key in scene_secret_names),
        )
        if value
    }


def _sensitive_environment_values(values: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in values.items()
        if re.search(
            r"(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE_KEY)(?:_|$)",
            key,
        )
        or key.endswith("_ACCOUNTS_JSON")
        or key == "DATABASE_URL"
        or key.endswith("_DATABASE_URL")
    }


def _runtime_exposure(
    request: backup_contract.BackupRequest,
    *,
    require_source_purged: bool,
    cloudflared_image: str,
    database_image: str,
    api_host_ip: str,
    api_host_port: int,
    api_container_port: int,
) -> dict[str, object]:
    registration, registration_raw = _strict_env(
        REGISTRATION_ENV,
        expected_keys=MAIL_KEYS,
    )
    google, google_raw = _strict_env(GOOGLE_ENV, expected_keys=GOOGLE_KEYS)
    scene_sensitive, scene_digest = _sensitive_scene_values()
    database_raw = _read_regular(
        DATABASE_ENV,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    database_values = _database_environment(database_raw)
    admission_values, admission_raw = _strict_env(ADMISSION_ENV)
    if (
        set(admission_values) != {"PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"}
        or admission_values["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"]
        != database_values["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"]
    ):
        raise IsolationError("admission_environment_binding_invalid")
    root_raw = _read_regular(
        ROOT_ENV,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    root_values, _root_keys = _parse_env(root_raw, path=ROOT_ENV)
    interpolation_values: dict[str, str] = {}
    for source in (
        root_values,
        scene_sensitive,
        database_values,
        admission_values,
        google,
        registration,
    ):
        interpolation_values.update(source)
    api_governed_render = {
        "PROPERTYQUARRY_GOVERNED_RENDER_API_URL": interpolation_values.get(
            "PROPERTYQUARRY_GOVERNED_RENDER_API_URL",
            "",
        ),
        "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN": interpolation_values.get(
            "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN",
            "",
        ),
        "PROPERTYQUARRY_GOVERNED_RENDER_API_TOKEN_FILE": "",
        "PROPERTYQUARRY_GOVERNED_RENDER_ALLOWED_ORIGIN": interpolation_values.get(
            "PROPERTYQUARRY_GOVERNED_RENDER_ALLOWED_ORIGIN",
            "",
        ),
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET": interpolation_values.get(
            "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET",
            "",
        ),
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_SIGNING_SECRET_FILE": "",
        "PROPERTYQUARRY_GOVERNED_RENDER_CONSENT_STORE_DIR": (
            "/data/governed-render-consents"
        ),
        "PROPERTYQUARRY_GOVERNED_RENDER_LOCALE": (
            interpolation_values.get("PROPERTYQUARRY_GOVERNED_RENDER_LOCALE")
            or "en-US"
        ),
    }
    root_sensitive = _sensitive_environment_values(root_values)
    root_authority_by_container = {
        "propertyquarry-api-live": {
            key: interpolation_values.get(key, "")
            for key in (
                "EA_API_TOKEN",
                "EA_PROVIDER_SECRET_KEY",
                "EA_SIGNING_SECRET",
                "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY",
                "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET",
                "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET",
                "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET",
                "PROPERTYQUARRY_RELEASE_PROBE_SECRET",
            )
        },
        "propertyquarry-worker-live": {
            key: interpolation_values.get(key, "")
            for key in ("EA_API_TOKEN", "EA_SIGNING_SECRET")
        },
        "propertyquarry-scheduler-live": {
            key: interpolation_values.get(key, "")
            for key in (
                "EA_API_TOKEN",
                "EA_SIGNING_SECRET",
                "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY",
                "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET",
                "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET",
                "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET",
            )
        },
        "propertyquarry-db-live": {
            "POSTGRES_PASSWORD": interpolation_values.get("POSTGRES_PASSWORD", "")
        },
        "propertyquarry-cloudflared-live": {
            "TUNNEL_TOKEN": interpolation_values.get(
                "PROPERTYQUARRY_CF_TUNNEL_TOKEN",
                "",
            )
        },
    }
    containers = _all_containers()
    stale = sorted(
        name
        for name in containers
        if (name.startswith("propertyquarry-") or name.startswith("pq-"))
        and name not in ALLOWED_PROPERTYQUARRY_CONTAINERS
    )
    if stale:
        raise IsolationError("stale_propertyquarry_containers_present", ",".join(stale))
    missing = sorted(set(EXPECTED_CONTAINERS) - set(containers))
    if missing:
        raise IsolationError("required_propertyquarry_container_missing", ",".join(missing))
    missing_one_shot = sorted(set(EXPECTED_ONE_SHOT_CONTAINERS) - set(containers))
    if missing_one_shot:
        raise IsolationError(
            "required_propertyquarry_one_shot_missing",
            ",".join(missing_one_shot),
        )

    evidence: list[dict[str, object]] = []
    observed_property_volumes: set[str] = set()
    property_names = set(EXPECTED_CONTAINERS)
    property_namespace_targets = set(property_names) | set(
        EXPECTED_ONE_SHOT_CONTAINERS
    )
    property_namespace_targets.update(
        str(container.get("Id") or "")
        for name, container in containers.items()
        if name in property_namespace_targets
        and re.fullmatch(r"[0-9a-f]{64}", str(container.get("Id") or ""))
    )
    protected_values = _protected_secret_values(
        registration,
        google,
        scene_sensitive,
    ) | {
        value
        for value in (*database_values.values(), *root_sensitive.values())
        if value
    }
    external_protected_keys = {*MAIL_KEYS, *GOOGLE_KEYS}
    external_protected_values = _protected_secret_values(
        registration,
        google,
        {},
    ) | {
        *database_values.values(),
        scene_sensitive[RENDER_BRIDGE_TOKEN_KEY],
    }
    expected_image_references = {
        request.web_image,
        request.render_image,
        database_image,
        cloudflared_image,
    }
    image_identities = _image_identities(tuple(expected_image_references))
    property_volume_mountpoints = _property_volume_mountpoints()
    for name, container in containers.items():
        networks = _container_networks(container, name=name)
        volumes = _container_volumes(container, name=name)
        is_property = name in property_names or name in EXPECTED_ONE_SHOT_CONTAINERS
        if not is_property and networks & PROPERTY_NETWORKS:
            raise IsolationError("propertyquarry_network_shared", name)
        if not is_property and any(
            volume.startswith(PROPERTY_VOLUME_PREFIX) for volume in volumes
        ):
            raise IsolationError("propertyquarry_volume_shared", name)
        if not is_property:
            host = container.get("HostConfig")
            if not isinstance(host, dict):
                raise IsolationError("container_host_config_invalid", name)
            if any(
                str(host.get(field) or "").startswith("container:")
                and str(host.get(field) or "").split(":", 1)[1]
                in property_namespace_targets
                for field in ("NetworkMode", "PidMode", "IpcMode")
            ):
                raise IsolationError("propertyquarry_namespace_shared", name)
            volumes_from = host.get("VolumesFrom")
            if volumes_from is not None and (
                not isinstance(volumes_from, list)
                or any(
                    str(item).split(":", 1)[0] in property_namespace_targets
                    for item in volumes_from
                )
            ):
                raise IsolationError("propertyquarry_volumes_from_shared", name)
            environment = _container_env(container, name=name)
            if (
                any(
                    key in external_protected_keys
                    or key.startswith("PROPERTYQUARRY_")
                    for key in environment
                )
                or any(
                    value and value in external_protected_values
                    for value in environment.values()
                )
            ):
                raise IsolationError("propertyquarry_secret_exposed", name)
            mounts = container.get("Mounts")
            if not isinstance(mounts, list):
                raise IsolationError("container_mounts_invalid", name)
            if any(
                isinstance(mount, dict) and _protected_bind_overlap(mount)
                for mount in mounts
            ):
                raise IsolationError("propertyquarry_bind_shared", name)
            if any(
                isinstance(mount, dict)
                and mount.get("Type") == "bind"
                and any(
                    _paths_overlap(str(mount.get(field) or ""), Path(mountpoint))
                    for field in ("Source", "Destination")
                    for mountpoint in property_volume_mountpoints.values()
                )
                for mount in mounts
            ):
                raise IsolationError("propertyquarry_volume_bind_shared", name)
        if is_property and any(
            volume.startswith("ea_")
            or "myexternalbrain" in volume.lower()
            or volume.startswith("/docker/EA")
            for volume in volumes
        ):
            raise IsolationError("externalbrain_volume_attached", name)
        if is_property:
            mounts = container.get("Mounts")
            if not isinstance(mounts, list):
                raise IsolationError("container_mounts_invalid", name)
            if any(
                isinstance(mount, dict)
                and mount.get("Type") == "bind"
                and any(
                    _paths_overlap(
                        str(mount.get(field) or ""),
                        Path("/var/lib/docker/volumes"),
                    )
                    for field in ("Source", "Destination")
                )
                for mount in mounts
            ):
                raise IsolationError("external_volume_bind_attached", name)

    for name, (service, image_kind) in EXPECTED_CONTAINERS.items():
        container = containers[name]
        config = container.get("Config")
        state = container.get("State")
        if not isinstance(config, dict) or not isinstance(state, dict):
            raise IsolationError("container_contract_invalid", name)
        labels = config.get("Labels")
        if not isinstance(labels, dict):
            raise IsolationError("container_labels_invalid", name)
        if (
            labels.get("com.docker.compose.project") != "property"
            or labels.get("com.docker.compose.service") != service
        ):
            raise IsolationError("container_compose_identity_invalid", name)
        if state.get("Status") != "running":
            raise IsolationError("container_not_running", name)
        health = state.get("Health")
        health_status = "not-configured"
        if name in REQUIRED_HEALTH_CONTAINERS:
            if not isinstance(health, dict):
                raise IsolationError("container_health_missing", name)
            health_status = str(health.get("Status") or "")
            if health_status != "healthy":
                raise IsolationError("container_not_healthy", name)
        elif health is not None:
            raise IsolationError("container_health_unexpected", name)
        expected_networks = EXPECTED_NETWORKS[name]
        networks = _container_networks(container, name=name)
        if networks != expected_networks:
            raise IsolationError("container_network_contract_invalid", name)
        mounts = _container_mount_contract(container, name=name)
        if mounts != EXPECTED_MOUNTS[name]:
            raise IsolationError("container_mount_contract_invalid", name)
        observed_property_volumes.update(
            source
            for mount_type, source, _destination, _writable in mounts
            if mount_type == "volume"
        )
        image = str(config.get("Image") or "")
        container_id = str(container.get("Id") or "")
        expected_image = _expected_image(
            image_kind,
            request=request,
            cloudflared_image=cloudflared_image,
            database_image=database_image,
        )
        if (
            image != expected_image
            or container.get("Image") != image_identities[expected_image]
            or not re.fullmatch(r"[0-9a-f]{64}", container_id)
        ):
            raise IsolationError("container_image_identity_invalid", name)
        _verify_process_contract(
            container,
            name=name,
            api_host_ip=api_host_ip,
            api_host_port=api_host_port,
            api_container_port=api_container_port,
        )
        env = _container_env(container, name=name)
        if (
            _propertyquarry_key_digest(env)
            != EXPECTED_PROPERTYQUARRY_ENV_KEY_DIGESTS[name]
        ):
            raise IsolationError("container_propertyquarry_env_keys_invalid", name)
        if image_kind in {"web", "render"} and env.get(
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA"
        ) != request.runtime_sha:
            raise IsolationError("container_runtime_sha_invalid", name)
        runtime_contracts = {
            "propertyquarry-api-live": (
                "api",
                "PROPERTYQUARRY_API_DATABASE_URL",
            ),
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
        if name in runtime_contracts:
            expected_role, database_key = runtime_contracts[name]
            if (
                env.get("EA_ROLE") != expected_role
                or env.get("EA_RUNTIME_MODE") != "prod"
            ):
                raise IsolationError("container_runtime_role_invalid", name)
            if env.get("DATABASE_URL") != database_values[database_key]:
                raise IsolationError("container_database_role_invalid", name)
        if name == "propertyquarry-api-live" and env.get(
            "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"
        ) != database_values["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"]:
            raise IsolationError("api_admission_database_role_invalid")
        if name in {
            "propertyquarry-api-live",
            "propertyquarry-worker-live",
            "propertyquarry-scheduler-live",
        } and env.get(
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
        ) != database_values["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"]:
            raise IsolationError("container_erasure_secret_invalid", name)
        allowed_protected: dict[str, str] = {}
        root_authority = root_authority_by_container.get(name, {})
        if any(env.get(key) != value for key, value in root_authority.items()):
            raise IsolationError("container_root_authority_invalid", name)
        allowed_protected.update(root_authority)
        if name == "propertyquarry-api-live":
            allowed_protected.update(registration)
            allowed_protected.update(google)
            allowed_protected[RENDER_BRIDGE_TOKEN_KEY] = scene_sensitive[
                RENDER_BRIDGE_TOKEN_KEY
            ]
            allowed_protected["DATABASE_URL"] = database_values[
                "PROPERTYQUARRY_API_DATABASE_URL"
            ]
            allowed_protected["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"] = (
                database_values["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"]
            )
            allowed_protected["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"] = (
                database_values["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"]
            )
            allowed_protected.update(api_governed_render)
        elif name == "propertyquarry-render-live":
            allowed_protected["DATABASE_URL"] = database_values[
                "PROPERTYQUARRY_RENDER_DATABASE_URL"
            ]
            allowed_protected.update(
                {
                    key: value
                    for key, value in scene_sensitive.items()
                    if key not in SCENE_RENDER_BLANK_KEYS
                    and key not in SCENE_ROLE_OVERRIDE_KEYS
                }
            )
        elif name == "propertyquarry-worker-live":
            allowed_protected.update(
                {
                    "DATABASE_URL": database_values[
                        "PROPERTYQUARRY_WORKER_DATABASE_URL"
                    ],
                    "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": database_values[
                        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
                    ],
                }
            )
        elif name == "propertyquarry-scheduler-live":
            allowed_protected.update(
                {
                    "DATABASE_URL": database_values[
                        "PROPERTYQUARRY_SCHEDULER_DATABASE_URL"
                    ],
                    "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": database_values[
                        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
                    ],
                }
            )
        forbidden_product_prefixes = (
            "ANTHROPIC_",
            "EA_TELEGRAM_",
            "EA_WHATSAPP_",
            "EXTERNALBRAIN_",
            "GEMINI_",
            "MYEXTERNALBRAIN_",
            "OPENAI_",
            "PCLOUD_",
        )
        if any(
            key.startswith(forbidden_product_prefixes)
            and value
            and allowed_protected.get(key) != value
            for key, value in env.items()
        ):
            raise IsolationError("container_external_product_env_exposed", name)
        if any(
            value and allowed_protected.get(key) != value
            for key, value in _sensitive_environment_values(env).items()
        ):
            raise IsolationError("container_unexpected_secret_env_exposed", name)
        if name == "propertyquarry-api-live":
            for key, expected_value in api_governed_render.items():
                if env.get(key) != expected_value:
                    raise IsolationError("api_governed_render_env_invalid", key)
        if any(
            key in env
            and env[key] != ""
            and allowed_protected.get(key) != env[key]
            for key in root_sensitive
        ):
            raise IsolationError("container_root_secret_env_exposed", name)
        if any(
            value
            and value in protected_values
            and allowed_protected.get(key) != value
            for key, value in env.items()
        ):
            raise IsolationError("container_renamed_secret_env_exposed", name)
        if name == "propertyquarry-api-live":
            for key in (
                "PROPERTYQUARRY_API_DATABASE_URL",
                "PROPERTYQUARRY_MIGRATION_DATABASE_URL",
                "PROPERTYQUARRY_RENDER_DATABASE_URL",
                "PROPERTYQUARRY_SCHEDULER_DATABASE_URL",
                "PROPERTYQUARRY_WORKER_DATABASE_URL",
                "POSTGRES_PASSWORD",
            ):
                if env.get(key) != "":
                    raise IsolationError("api_database_placeholder_invalid", key)
            for key, value in registration.items():
                if env.get(key) != value:
                    raise IsolationError("api_registration_email_env_invalid", key)
            for key, value in google.items():
                if env.get(key) != value:
                    raise IsolationError("api_google_identity_env_invalid", key)
            bridge_value = scene_sensitive.get(RENDER_BRIDGE_TOKEN_KEY, "")
            if not bridge_value or env.get(RENDER_BRIDGE_TOKEN_KEY) != bridge_value:
                raise IsolationError("api_render_bridge_env_invalid")
            forbidden_scene = {
                key: value
                for key, value in scene_sensitive.items()
                if key != RENDER_BRIDGE_TOKEN_KEY
                and key not in SCENE_API_AUTHORITY_KEYS
                and key not in SCENE_ROLE_OVERRIDE_KEYS
            }
            leaked = [
                key
                for key in forbidden_scene
                if (
                    env.get(key) != ""
                    if key in SCENE_RENDER_BLANK_KEYS
                    else key in env
                )
            ]
            if leaked:
                raise IsolationError("api_render_provider_env_exposed", leaked[0])
        elif name == "propertyquarry-render-live":
            if any(key in env for key in (*MAIL_KEYS, *GOOGLE_KEYS)):
                raise IsolationError("render_identity_env_exposed", name)
            for key, value in scene_sensitive.items():
                if key in SCENE_ROLE_OVERRIDE_KEYS:
                    continue
                expected_value = "" if key in SCENE_RENDER_BLANK_KEYS else value
                if key not in env or env[key] != expected_value:
                    raise IsolationError("render_provider_env_invalid", key)
        elif name in {
            "propertyquarry-worker-live",
            "propertyquarry-scheduler-live",
        }:
            if any(key in env for key in (*MAIL_KEYS, *GOOGLE_KEYS)):
                raise IsolationError("background_identity_env_exposed", name)
            leaked = [
                key
                for key in scene_sensitive
                if key not in SCENE_ROLE_OVERRIDE_KEYS
                and (
                    env.get(key) != ""
                    if key in SCENE_RENDER_BLANK_KEYS
                    else key in env
                )
            ]
            if leaked:
                raise IsolationError("background_render_provider_env_exposed", leaked[0])
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
                    if env.get(key) != "":
                        raise IsolationError(
                            "scheduler_database_placeholder_invalid",
                            key,
                        )
        elif name in {
            "propertyquarry-db-live",
            "propertyquarry-cloudflared-live",
        }:
            if any(
                key in env
                for key in (*MAIL_KEYS, *GOOGLE_KEYS, *scene_sensitive)
            ):
                raise IsolationError("infrastructure_secret_env_exposed", name)
        evidence.append(
            {
                "compose_service": service,
                "container_id": container_id,
                "health": health_status,
                "image": image,
                "image_id": str(container.get("Image") or ""),
                "name": name,
                "networks": sorted(networks),
                "repo_digest": _canonical_repo_digest(expected_image),
                "volumes": sorted(_container_volumes(container, name=name)),
            }
        )

    if observed_property_volumes != PROPERTY_VOLUMES:
        raise IsolationError("propertyquarry_volume_set_invalid")

    one_shot_evidence: list[dict[str, object]] = []
    for name, service in EXPECTED_ONE_SHOT_CONTAINERS.items():
        container = containers[name]
        config = container.get("Config")
        state = container.get("State")
        if not isinstance(config, dict) or not isinstance(state, dict):
            raise IsolationError("container_contract_invalid", name)
        labels = config.get("Labels")
        if (
            not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != "property"
            or labels.get("com.docker.compose.service") != service
        ):
            raise IsolationError("container_compose_identity_invalid", name)
        if state.get("Status") != "exited" or state.get("ExitCode") != 0:
            raise IsolationError("one_shot_not_successful", name)
        if str(config.get("Image") or "") != request.web_image:
            raise IsolationError("container_web_image_invalid", name)
        if (
            container.get("Image") != image_identities[request.web_image]
            or not re.fullmatch(r"[0-9a-f]{64}", str(container.get("Id") or ""))
        ):
            raise IsolationError("container_image_identity_invalid", name)
        _verify_process_contract(
            container,
            name=name,
            api_host_ip=api_host_ip,
            api_host_port=api_host_port,
            api_container_port=api_container_port,
        )
        if _container_networks(container, name=name) != {"property_default"}:
            raise IsolationError("container_network_contract_invalid", name)
        if _container_mount_contract(container, name=name):
            raise IsolationError("one_shot_mount_contract_invalid", name)
        env = _container_env(container, name=name)
        if (
            _propertyquarry_key_digest(env)
            != EXPECTED_PROPERTYQUARRY_ENV_KEY_DIGESTS[name]
        ):
            raise IsolationError("container_propertyquarry_env_keys_invalid", name)
        if env.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") != request.runtime_sha:
            raise IsolationError("container_runtime_sha_invalid", name)
        if (
            env.get("EA_ROLE") != "property-search-migrate"
            or env.get("EA_RUNTIME_MODE") != "prod"
        ):
            raise IsolationError("container_runtime_role_invalid", name)
        if env.get("DATABASE_URL") != database_values[
            "PROPERTYQUARRY_MIGRATION_DATABASE_URL"
        ]:
            raise IsolationError("container_database_role_invalid", name)
        if env.get(
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
        ) != database_values["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"]:
            raise IsolationError("container_erasure_secret_invalid", name)
        one_shot_scene_keys = (
            key for key in scene_sensitive if key not in SCENE_ROLE_OVERRIDE_KEYS
        )
        if any(key in env for key in (*MAIL_KEYS, *GOOGLE_KEYS, *one_shot_scene_keys)):
            raise IsolationError("one_shot_secret_env_exposed", name)
        allowed_protected = {
            "DATABASE_URL": database_values[
                "PROPERTYQUARRY_MIGRATION_DATABASE_URL"
            ],
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": database_values[
                "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
            ],
        }
        if any(
            value and allowed_protected.get(key) != value
            for key, value in _sensitive_environment_values(env).items()
        ):
            raise IsolationError("one_shot_unexpected_secret_env_exposed", name)
        if any(
            value
            and value in protected_values
            and allowed_protected.get(key) != value
            for key, value in env.items()
        ):
            raise IsolationError("one_shot_renamed_secret_env_exposed", name)
        one_shot_evidence.append(
            {
                "compose_service": service,
                "container_id": str(container.get("Id") or ""),
                "exit_code": 0,
                "image": str(config.get("Image") or ""),
                "image_id": str(container.get("Image") or ""),
                "name": name,
                "networks": ["property_default"],
                "repo_digest": _canonical_repo_digest(request.web_image),
                "status": "exited",
            }
        )

    legacy_present = [key for key in MAIL_KEYS if key in root_values]
    if require_source_purged and legacy_present:
        raise IsolationError("legacy_registration_email_not_purged")
    if any(key in root_values for key in GOOGLE_KEYS):
        raise IsolationError("google_identity_present_in_legacy_env")
    return {
        "containers": sorted(evidence, key=lambda item: str(item["name"])),
        "admission_env_sha256": _sha256_id(admission_raw),
        "database_env_sha256": _sha256_id(database_raw),
        "google_env_sha256": _sha256_id(google_raw),
        "google_key_count": len(GOOGLE_KEYS),
        "legacy_registration_email_present": bool(legacy_present),
        "one_shot_containers": one_shot_evidence,
        "property_volume_mountpoints": dict(sorted(property_volume_mountpoints.items())),
        "registration_email_env_sha256": _sha256_id(registration_raw),
        "registration_email_key_count": len(MAIL_KEYS),
        "render_provider_env_sha256": scene_digest,
        "render_provider_key_count": len(scene_sensitive),
        "topology_isolated": True,
    }


def _verify_domain_receipt(
    path: Path,
    *,
    public_key: object,
    key_id: str,
    domain: bytes,
) -> tuple[dict[str, object], str]:
    raw = _read_regular(
        path,
        max_bytes=MAX_DATABASE_RECEIPT_BYTES,
        mode=0o600,
        uid=0,
        gid=0,
    )
    try:
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            raise ValueError("newline")
        wrapper = json.loads(raw[:-1])
        if not isinstance(wrapper, dict) or set(wrapper) != {
            "payload",
            "signature",
            "signature_key_id",
        }:
            raise ValueError("wrapper")
        if raw != _canonical_json(wrapper) + b"\n":
            raise ValueError("canonical")
        payload = wrapper["payload"]
        if not isinstance(payload, dict) or wrapper["signature_key_id"] != key_id:
            raise ValueError("payload")
        signature_text = str(wrapper["signature"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{86}", signature_text):
            raise ValueError("signature")
        signature = base64.b64decode(
            signature_text + "==",
            altchars=b"-_",
            validate=True,
        )
        if len(signature) != 64:
            raise ValueError("signature")
        encoded = _canonical_json(payload)
        public_key.verify(  # type: ignore[attr-defined]
            signature,
            domain + len(encoded).to_bytes(8, byteorder="big", signed=False) + encoded,
        )
    except Exception as exc:
        raise IsolationError("signed_receipt_invalid", str(path)) from exc
    return dict(payload), _sha256_id(raw)


def _read_isolation_predecessor(
    operation: str,
    request: backup_contract.BackupRequest,
    *,
    bindings: Mapping[str, object],
    public_key: object,
    key_id: str,
) -> tuple[dict[str, object], str]:
    path = (
        RECEIPT_ROOT
        / request.runtime_sha
        / request.deployment_id
        / f"{operation}.json"
    )
    payload, digest = _verify_domain_receipt(
        path,
        public_key=public_key,
        key_id=key_id,
        domain=SIGNATURE_DOMAIN,
    )
    expected = {
        **_receipt_binding_fields(bindings),
        "deployment_id": request.deployment_id,
        "envelope_sha": request.envelope_sha,
        "operation": operation,
        "render_image": request.render_image,
        "runtime_sha": request.runtime_sha,
        "schema": SCHEMA,
        "web_image": request.web_image,
    }
    if (
        any(payload.get(key) != value for key, value in expected.items())
        or payload.get("status") != "verified"
        or payload.get("production_ready") is not False
        or payload.get("secret_values_emitted") is not False
        or payload.get("receipt_authority_key_id") != key_id
    ):
        raise IsolationError("isolation_predecessor_binding_invalid", operation)
    started = _exact_int(payload.get("started_at_epoch"), 1, (1 << 62) - 1)
    finished = _exact_int(payload.get("finished_at_epoch"), 1, (1 << 62) - 1)
    if finished < started or finished - started > int(
        bindings["backup_max_age_seconds"]
    ):
        raise IsolationError("isolation_predecessor_time_invalid", operation)
    return payload, digest


def _read_deploy_receipt(
    request: backup_contract.BackupRequest,
    *,
    bindings: Mapping[str, object],
    public_key: object,
    key_id: str,
    backup_digest: str,
    purge_digest: str,
    retirement_digest: str,
    database_receipts: Mapping[str, object],
    database_substrate: Mapping[str, object],
    earliest_started_at: int,
) -> tuple[dict[str, object], str]:
    path = (
        deploy_contract.DEPLOY_RECEIPT_ROOT
        / request.runtime_sha
        / request.deployment_id
        / "deploy-runtime.json"
    )
    payload, digest = _verify_domain_receipt(
        path,
        public_key=public_key,
        key_id=key_id,
        domain=deploy_contract.SIGNATURE_DOMAIN,
    )
    receipt_digests = {
        operation: str(item.get("receipt_sha256") or "")
        for operation, item in database_receipts.items()
        if isinstance(item, dict)
    }
    installed_bindings = {
        key: bindings[key] for key in deploy_contract.INSTALLED_BINDING_KEYS
    }
    expected = {
        **installed_bindings,
        "api_container_port": int(bindings["api_container_port"]),
        "api_host_ip": str(bindings["api_host_ip"]),
        "api_host_port": int(bindings["api_host_port"]),
        "backup_max_age_seconds": int(bindings["backup_max_age_seconds"]),
        "backup_receipt_sha256": backup_digest,
        "cloudflared_image": str(bindings["cloudflared_image"]),
        "database_container": "propertyquarry-db-live",
        "database_container_id": database_substrate["container_id"],
        "database_image": request.database_image,
        "database_image_id": database_substrate["image_id"],
        "database_oid": database_substrate["database_oid"],
        "database_pgdata_volume": database_substrate["pgdata_volume"],
        "database_receipts": receipt_digests,
        "database_repo_digest": database_substrate["repo_digest"],
        "deployment_id": request.deployment_id,
        "envelope_sha": request.envelope_sha,
        "host_machine_id_digest": backup_contract._machine_id_digest(  # noqa: SLF001
            backup_contract.MACHINE_ID_PATH
        ),
        "operation": "deploy-runtime",
        "purge_receipt_sha256": purge_digest,
        "receipt_authority_key_id": key_id,
        "render_image": request.render_image,
        "retirement_receipt_sha256": retirement_digest,
        "runtime_inputs": bindings["runtime_inputs"],
        "runtime_retirement_digest": bindings["runtime_retirement_digest"],
        "runtime_sha": request.runtime_sha,
        "schema": deploy_contract.SCHEMA,
        "status": "verified",
        "transaction_started_at_epoch": bindings[
            "transaction_started_at_epoch"
        ],
        "web_image": request.web_image,
    }
    environment_digests = {
        str(item["path"]): str(item["sha256"])
        for item in bindings["runtime_inputs"]
        if isinstance(item, dict)
    }
    if (
        set(payload) != deploy_contract.DEPLOY_PAYLOAD_KEYS
        or any(payload.get(key) != value for key, value in expected.items())
        or payload.get("environment_digests") != dict(
            sorted(environment_digests.items())
        )
        or _sha256_id(_canonical_json(payload.get("runtime_deploy")))
        != bindings.get("runtime_deploy_digest")
        or payload.get("pre_observations") != payload.get("post_observations")
        or payload.get("build_performed") is not False
        or payload.get("exit_code") != 0
        or payload.get("idempotent") is not True
        or payload.get("mutation") is not True
        or payload.get("orphans_removed") is not False
        or payload.get("output_redacted") is not True
        or payload.get("production_ready") is not False
        or payload.get("pull_policy") != "always"
        or payload.get("secret_values_emitted") is not False
        or payload.get("wait_completed") is not True
        or payload.get("subprocess_timeout_seconds") != 1800
    ):
        raise IsolationError("deploy_receipt_binding_invalid")
    started = _exact_int(payload.get("started_at_epoch"), 1, (1 << 62) - 1)
    finished = _exact_int(payload.get("finished_at_epoch"), 1, (1 << 62) - 1)
    duration = _exact_int(payload.get("duration_seconds"), 0, 1800)
    if (
        started < earliest_started_at
        or finished < started
        or finished - started != duration
        or finished > int(time.time()) + 30
    ):
        raise IsolationError("deploy_receipt_time_invalid")
    for prefix in ("stdout", "stderr"):
        _exact_int(payload.get(f"{prefix}_bytes"), 0, 64 * 1024 * 1024)
        if not SHA256_ID_RE.fullmatch(str(payload.get(f"{prefix}_sha256") or "")):
            raise IsolationError("deploy_receipt_output_invalid", prefix)
    return payload, digest


def _exact_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IsolationError("database_receipt_integer_invalid")
    if value < minimum or value > maximum:
        raise IsolationError("database_receipt_integer_invalid")
    return value


def _database_environment(raw: bytes) -> dict[str, str]:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n") or b"\x00" in raw or b"\r" in raw:
        raise IsolationError("database_environment_format_invalid")
    lines = raw[:-1].split(b"\n")
    if len(lines) != len(DATABASE_ENV_KEYS):
        raise IsolationError("database_environment_shape_invalid")
    values: dict[str, str] = {}
    for expected, line in zip(DATABASE_ENV_KEYS, lines, strict=True):
        key, separator, encoded = line.partition(b"=")
        if separator != b"=" or key.decode("ascii", errors="strict") != expected:
            raise IsolationError("database_environment_shape_invalid")
        try:
            value = encoded.decode("ascii")
        except UnicodeDecodeError as exc:
            raise IsolationError("database_environment_value_invalid", expected) from exc
        if not value or len(value) > 2048:
            raise IsolationError("database_environment_value_invalid", expected)
        values[expected] = value
    password = r"[A-Za-z0-9_-]{48,128}"
    patterns = {
        "PROPERTYQUARRY_API_DATABASE_URL": rf"postgresql://propertyquarry_api:{password}@propertyquarry-db:5432/propertyquarry",
        "PROPERTYQUARRY_SCHEDULER_DATABASE_URL": rf"postgresql://propertyquarry_scheduler:{password}@propertyquarry-db:5432/propertyquarry",
        "PROPERTYQUARRY_WORKER_DATABASE_URL": rf"postgresql://propertyquarry_worker:{password}@propertyquarry-db:5432/propertyquarry",
        "PROPERTYQUARRY_MIGRATION_DATABASE_URL": rf"postgresql://propertyquarry_migrator:{password}@propertyquarry-db:5432/propertyquarry\?options=-c%20role%3Dpropertyquarry_owner%20-c%20search_path%3Dpublic%2Cpg_catalog",
    }
    if any(re.fullmatch(pattern, values[key]) is None for key, pattern in patterns.items()):
        raise IsolationError("database_environment_url_invalid")
    admission = values["PROPERTYQUARRY_API_ADMISSION_DATABASE_URL"]
    if (
        re.fullmatch(
            rf"postgresql://propertyquarry_admission_runtime:{password}@propertyquarry-db:5432/propertyquarry_admission",
            admission,
        )
        is None
        or values["PROPERTYQUARRY_RENDER_DATABASE_URL"] != admission
        or re.fullmatch(
            password,
            values["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"],
        )
        is None
    ):
        raise IsolationError("database_environment_admission_invalid")
    return values


def _valid_versions(
    value: object,
    *,
    minimum_exclusive: int,
    current: int,
    allow_empty_at_current: bool,
) -> bool:
    if not isinstance(value, list):
        return False
    if not value:
        return allow_empty_at_current and minimum_exclusive == current
    previous = minimum_exclusive
    for item in value:
        try:
            version = _exact_int(item, minimum_exclusive + 1, current)
        except IsolationError:
            return False
        if version <= previous:
            return False
        previous = version
    return previous == current


def _validate_database_migration(schema: Mapping[str, object]) -> str:
    if set(schema) != {"google_identity", "kernel", "property_search", "status"} or schema.get("status") != "migrated":
        raise IsolationError("database_receipt_migration_shape_invalid")
    components = {
        "kernel": "ea_kernel",
        "property_search": "property_search",
        "google_identity": "propertyquarry_google_identity",
    }
    for field, component in components.items():
        value = schema.get(field)
        if not isinstance(value, dict) or set(value) != {
            "applied_versions",
            "component",
            "current_version",
            "previous_version",
        } or value.get("component") != component:
            raise IsolationError("database_receipt_migration_component_invalid", field)
        previous = _exact_int(value.get("previous_version"), 0, (1 << 31) - 1)
        current = _exact_int(value.get("current_version"), 1, (1 << 31) - 1)
        if current < previous or not _valid_versions(
            value.get("applied_versions"),
            minimum_exclusive=previous,
            current=current,
            allow_empty_at_current=True,
        ):
            raise IsolationError("database_receipt_migration_component_invalid", field)
    return "migrated"


def _validate_database_readiness(schema: Mapping[str, object]) -> str:
    if set(schema) != {
        "google_identity",
        "kernel",
        "property_search",
        "ready",
        "status",
    } or schema.get("ready") is not True or schema.get("status") != "ready":
        raise IsolationError("database_receipt_readiness_shape_invalid")
    components = {
        "kernel": "ea_kernel",
        "property_search": "property_search",
        "google_identity": "propertyquarry_google_identity",
    }
    for field, component in components.items():
        value = schema.get(field)
        if not isinstance(value, dict) or set(value) != {
            "applied_versions",
            "component",
            "current_version",
            "ready",
            "reason",
            "required_version",
        } or value.get("component") != component or value.get("ready") is not True or value.get("reason") != "ready":
            raise IsolationError("database_receipt_readiness_component_invalid", field)
        current = _exact_int(value.get("current_version"), 1, (1 << 31) - 1)
        required = _exact_int(value.get("required_version"), 1, (1 << 31) - 1)
        if current != required or not _valid_versions(
            value.get("applied_versions"),
            minimum_exclusive=0,
            current=current,
            allow_empty_at_current=False,
        ):
            raise IsolationError("database_receipt_readiness_component_invalid", field)
    return "ready"


def _validate_database_result(operation: str, result: object) -> tuple[int, str]:
    if not isinstance(result, dict):
        raise IsolationError("database_receipt_result_invalid", operation)
    if operation == "provision-roles":
        if set(result) != {"credential_reused", "database_oid", "roles"} or not isinstance(result.get("credential_reused"), bool) or result.get("roles") != list(DATABASE_ROLES):
            raise IsolationError("database_receipt_provision_result_invalid")
        return _exact_int(result.get("database_oid"), 1, (1 << 62) - 1), "provisioned"
    if set(result) != {"credential_reused", "database_oid", "schema"} or result.get("credential_reused") is not True:
        raise IsolationError("database_receipt_schema_result_invalid", operation)
    database_oid = _exact_int(result.get("database_oid"), 1, (1 << 62) - 1)
    schema = result.get("schema")
    if not isinstance(schema, dict):
        raise IsolationError("database_receipt_schema_result_invalid", operation)
    if operation == "migrate-schema":
        return database_oid, _validate_database_migration(schema)
    if operation in {"harden-runtime-acl", "verify-schema-readiness"}:
        return database_oid, _validate_database_readiness(schema)
    raise IsolationError("database_receipt_operation_invalid", operation)


def _database_receipts(
    request: backup_contract.BackupRequest,
    *,
    bindings: Mapping[str, object],
    public_key: object,
    key_id: str,
    earliest_started_at: int,
    expected_substrate: Mapping[str, object],
    backup_digest: str,
    purge_digest: str,
    retirement_digest: str,
) -> dict[str, object]:
    env_raw = _read_regular(
        DATABASE_ENV,
        max_bytes=MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    _database_environment(env_raw)
    env_digest = _sha256_id(env_raw)
    machine_digest = backup_contract._machine_id_digest(  # noqa: SLF001
        backup_contract.MACHINE_ID_PATH
    )
    try:
        database_substrate = backup_contract._validated_database_substrate(  # noqa: SLF001
            expected_substrate
        )
    except backup_contract.BackupError as exc:
        raise IsolationError("database_substrate_invalid", exc.code) from exc
    if (
        database_substrate != bindings.get("database_substrate")
        or database_substrate.get("image") != bindings.get("database_image")
    ):
        raise IsolationError("database_substrate_chain_invalid")
    for digest in (backup_digest, purge_digest, retirement_digest):
        if not SHA256_ID_RE.fullmatch(digest):
            raise IsolationError("database_predecessor_digest_invalid")
    observed: dict[str, object] = {}
    migration_versions: dict[str, int] | None = None
    hardened_readiness: dict[str, tuple[int, int, tuple[int, ...]]] | None = None
    previous_finished = _exact_int(earliest_started_at, 1, (1 << 62) - 1)
    predecessor_digest = retirement_digest
    now = int(time.time())
    for operation in DATABASE_OPERATIONS:
        path = (
            DATABASE_RECEIPT_ROOT
            / request.runtime_sha
            / request.deployment_id
            / f"{operation}.json"
        )
        payload, digest = _verify_domain_receipt(
            path,
            public_key=public_key,
            key_id=key_id,
            domain=DATABASE_SIGNATURE_DOMAIN,
        )
        expected_bindings = {
            "authority_digest": bindings.get("authority_digest"),
            "backup_max_age_seconds": bindings.get("backup_max_age_seconds"),
            "backup_receipt_sha256": backup_digest,
            "database": "propertyquarry",
            "database_container": "propertyquarry-db-live",
            "database_image": bindings.get("database_image"),
            "database_image_id": database_substrate["image_id"],
            "database_repo_digest": database_substrate["repo_digest"],
            "database_substrate_after": database_substrate,
            "database_substrate_before": database_substrate,
            "deployment_id": request.deployment_id,
            "docker_network": "property_default",
            "env_file": str(DATABASE_ENV),
            "env_file_sha256": env_digest,
            "host_machine_id_digest": machine_digest,
            "operation": operation,
            "predecessor_receipt_sha256": predecessor_digest,
            "purge_receipt_sha256": purge_digest,
            "receipt_authority_key_id": key_id,
            "retirement_receipt_sha256": retirement_digest,
            "runtime_inputs": bindings.get("runtime_inputs"),
            "runtime_sha": request.runtime_sha,
            "schema": "propertyquarry.database-control-receipt.v2",
            "status": "verified",
            "transaction_started_at_epoch": bindings.get(
                "transaction_started_at_epoch"
            ),
            "web_image": request.web_image,
        }
        if (
            set(payload) != DATABASE_PAYLOAD_KEYS
            or any(
                payload.get(field) != expected
                for field, expected in expected_bindings.items()
            )
            or payload.get("production_ready") is not False
            or payload.get("secret_values_emitted") is not False
        ):
            raise IsolationError("database_receipt_binding_invalid", operation)
        started = _exact_int(payload.get("started_at_epoch"), 1, (1 << 62) - 1)
        finished = _exact_int(payload.get("finished_at_epoch"), 1, (1 << 62) - 1)
        if (
            finished < started
            or started < previous_finished
            or finished > now + 30
            or finished - started > 7200
        ):
            raise IsolationError("database_receipt_time_invalid", operation)
        result = payload.get("result")
        database_oid, schema_status = _validate_database_result(
            operation,
            result,
        )
        if database_oid != database_substrate["database_oid"]:
            raise IsolationError("database_receipt_oid_continuity_invalid")
        if operation != "provision-roles":
            if not isinstance(result, dict) or not isinstance(result.get("schema"), dict):
                raise IsolationError("database_receipt_schema_result_invalid", operation)
            schema = result["schema"]
            current_versions = {
                field: int(schema[field]["current_version"])
                for field in ("google_identity", "kernel", "property_search")
            }
            if operation == "migrate-schema":
                migration_versions = current_versions
            else:
                if migration_versions is None or current_versions != migration_versions:
                    raise IsolationError(
                        "database_receipt_component_continuity_invalid",
                        operation,
                    )
                readiness = {
                    field: (
                        int(schema[field]["current_version"]),
                        int(schema[field]["required_version"]),
                        tuple(int(item) for item in schema[field]["applied_versions"]),
                    )
                    for field in ("google_identity", "kernel", "property_search")
                }
                if operation == "harden-runtime-acl":
                    hardened_readiness = readiness
                elif hardened_readiness is None or readiness != hardened_readiness:
                    raise IsolationError(
                        "database_receipt_component_continuity_invalid",
                        operation,
                    )
        previous_finished = finished
        observed[operation] = {
            "database_oid": database_oid,
            "database_image_id": str(payload["database_image_id"]),
            "database_repo_digest": str(payload["database_repo_digest"]),
            "env_file_sha256": env_digest,
            "finished_at_epoch": finished,
            "receipt_sha256": digest,
            "schema_status": schema_status,
            "started_at_epoch": started,
        }
        predecessor_digest = digest
    return dict(sorted(observed.items()))


def _local_http(*, host_port: int) -> dict[str, object]:
    checks: dict[str, object] = {}
    for path, required_fragments in (
        ("/health/ready", ()),
        ("/register", (b"Send verification code", b"Send again")),
        ("/sign-in", (b"sign-in-options",)),
    ):
        connection = http.client.HTTPConnection("127.0.0.1", host_port, timeout=15)
        try:
            connection.request(
                "GET",
                path,
                headers={"Host": "propertyquarry.com", "User-Agent": "pq-isolation-v2"},
            )
            response = connection.getresponse()
            body = response.read(2 * 1024 * 1024 + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise IsolationError("local_http_unavailable", path) from exc
        finally:
            connection.close()
        if response.status != 200 or len(body) > 2 * 1024 * 1024:
            raise IsolationError("local_http_status_invalid", path)
        if any(fragment not in body for fragment in required_fragments):
            raise IsolationError("local_http_content_invalid", path)
        checks[path] = {
            "body_sha256": _sha256_id(body),
            "bytes": len(body),
            "status": response.status,
        }
    return checks


def _sign_payload(
    payload: Mapping[str, object],
    *,
    private_key: object,
    key_id: str,
) -> dict[str, object]:
    payload_object = dict(payload)
    encoded = _canonical_json(payload_object)
    signature = private_key.sign(  # type: ignore[attr-defined]
        SIGNATURE_DOMAIN
        + len(encoded).to_bytes(8, byteorder="big", signed=False)
        + encoded
    )
    return {
        "payload": payload_object,
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        "signature_key_id": key_id,
    }


def _write_receipt(path: Path, wrapper: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != 0
        or metadata.st_gid != 0
    ):
        raise IsolationError("receipt_parent_invalid")
    encoded = _canonical_json(dict(wrapper)) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_signed_args(
    args: argparse.Namespace,
) -> tuple[str, Path]:
    operation = str(args.operation or "")
    runtime_sha = str(args.runtime_sha or "").strip()
    if operation not in SIGNED_OPERATIONS:
        raise IsolationError("operation_invalid")
    if not RUNTIME_SHA_RE.fullmatch(runtime_sha):
        raise IsolationError("runtime_sha_invalid")
    receipt = Path(args.receipt).resolve()
    deployment_id = str(args.deployment_id or "").strip()
    if not DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise IsolationError("deployment_id_invalid")
    expected = (
        RECEIPT_ROOT / runtime_sha / deployment_id / f"{operation}.json"
    )
    if receipt != expected:
        raise IsolationError("receipt_path_invalid")
    if os.geteuid() != 0:
        raise IsolationError("root_required")
    return operation, receipt


def execute_signed(args: argparse.Namespace) -> dict[str, object]:
    operation, receipt = _validate_signed_args(args)
    request, bindings, runtime_retirement, private, public, key_id = _contract(args)
    started = int(time.time())
    if operation in {
        "purge-legacy-runtime-exposure",
        "restore-legacy-runtime-exposure",
    }:
        _backup_payload, backup_digest = _read_backup_receipt(
            request,
            public_key=public,
            key_id=key_id,
            bindings=bindings,
        )
        _cleanup_atomic_user_temps(ROOT_ENV)
        expected_pre_purge_digest = str(
            args.pre_purge_root_env_digest or ""
        ).strip()
        if (
            not SHA256_ID_RE.fullmatch(expected_pre_purge_digest)
            or expected_pre_purge_digest
            != bindings.get("pre_purge_root_env_digest")
        ):
            raise IsolationError("pre_purge_root_env_digest_invalid")
        current_root_raw = _read_regular(
            ROOT_ENV,
            max_bytes=MAX_ENV_BYTES,
            mode=0o600,
            uid=1000,
            gid=1000,
        )
        current_root_values, _current_root_keys = _parse_env(
            current_root_raw,
            path=ROOT_ENV,
        )
        legacy_present = _mail_source_keys(current_root_values)
        current_runtime_inputs = _current_runtime_inputs()
        expected_current_inputs = (
            bindings.get("pre_purge_runtime_inputs")
            if legacy_present
            else bindings.get("runtime_inputs")
        )
        if current_runtime_inputs != expected_current_inputs:
            raise IsolationError("runtime_inputs_transition_invalid")
        artifact_path = _rollback_artifact_path(
            request.runtime_sha,
            request.deployment_id,
        )
        if legacy_present:
            pre_purge_inputs = _validate_runtime_inputs(require_legacy_mail=True)
            if (
                pre_purge_inputs["file_digests"].get(str(ROOT_ENV))
                != expected_pre_purge_digest
            ):
                raise IsolationError("pre_purge_root_env_digest_invalid")
            preimage, rollback_artifact = _ensure_rollback_artifact(
                runtime_sha=request.runtime_sha,
                deployment_id=request.deployment_id,
                preimage=current_root_raw,
                expected_pre_purge_digest=expected_pre_purge_digest,
            )
        elif artifact_path.exists():
            preimage, rollback_artifact = _load_rollback_artifact(
                runtime_sha=request.runtime_sha,
                deployment_id=request.deployment_id,
                expected_pre_purge_digest=expected_pre_purge_digest,
            )
        elif operation == "restore-legacy-runtime-exposure" and (
            _sha256_id(current_root_raw) == expected_pre_purge_digest
        ):
            preimage = current_root_raw
            rollback_artifact = None
        else:
            raise IsolationError("rollback_artifact_missing")
        preimage_values, _preimage_keys = _parse_env(preimage, path=ROOT_ENV)
        preimage_mail_keys = _mail_source_keys(preimage_values)
        if preimage_mail_keys not in (LEGACY_MAIL_KEYS, MAIL_KEYS):
            raise IsolationError("rollback_artifact_legacy_source_invalid")
        dedicated, _dedicated_raw = _strict_env(
            REGISTRATION_ENV,
            expected_keys=MAIL_KEYS,
        )
        _validate_mail_source_against_dedicated(
            preimage_values,
            preimage_mail_keys,
            dedicated,
            error_code="registration_email_source_mismatch",
        )
        postimage, expected_removed = _filtered_root_env(preimage)
        expected_post_purge_digest = _sha256_id(postimage)
        if expected_post_purge_digest != bindings.get(
            "post_purge_root_env_digest"
        ):
            raise IsolationError("post_purge_root_env_digest_invalid")
        current_digest = _sha256_id(current_root_raw)

        if operation == "restore-legacy-runtime-exposure":
            if current_digest == expected_post_purge_digest:
                _atomic_user_file(ROOT_ENV, preimage)
                restored = True
            elif current_digest == expected_pre_purge_digest:
                restored = False
            else:
                raise IsolationError("rollback_current_root_env_invalid")
            inputs = _validate_runtime_inputs(require_legacy_mail=True)
            restored_digest = str(inputs["file_digests"].get(str(ROOT_ENV)) or "")
            if restored_digest != expected_pre_purge_digest:
                raise IsolationError("rollback_restore_digest_invalid")
            if _current_runtime_inputs() != bindings.get(
                "pre_purge_runtime_inputs"
            ):
                raise IsolationError("rollback_runtime_inputs_invalid")
            result: dict[str, object] = {
                "backup_receipt_sha256": backup_digest,
                "expected_post_purge_root_env_digest": expected_post_purge_digest,
                "pre_purge_root_env_digest": expected_pre_purge_digest,
                "restored": restored,
                "restored_root_env_digest": restored_digest,
                "rollback_artifact": rollback_artifact,
                "runtime_inputs": inputs,
            }
        else:
            if current_digest == expected_pre_purge_digest:
                removed, root_digest = _purged_root_env()
            elif current_digest == expected_post_purge_digest:
                _validate_runtime_inputs(require_legacy_mail=False)
                removed, root_digest = 0, current_digest
            else:
                raise IsolationError("purge_current_root_env_invalid")
            if root_digest != expected_post_purge_digest:
                raise IsolationError("post_purge_root_env_digest_invalid")
            inputs = _validate_runtime_inputs(require_legacy_mail=False)
            if _current_runtime_inputs() != bindings.get("runtime_inputs"):
                raise IsolationError("post_purge_runtime_inputs_invalid")
            result = {
                "backup_receipt_sha256": backup_digest,
                "inputs": inputs,
                "legacy_keys_removed": removed,
                "post_purge_root_env_digest": root_digest,
                "pre_purge_root_env_digest": expected_pre_purge_digest,
                "rollback_artifact": rollback_artifact,
                "rollback_artifact_expected_removed_keys": expected_removed,
            }
    elif operation == RETIREMENT_OPERATION:
        _validate_runtime_inputs(require_legacy_mail=False)
        if _current_runtime_inputs() != bindings.get("runtime_inputs"):
            raise IsolationError("post_purge_runtime_inputs_invalid")
        backup_payload, backup_digest = _read_backup_receipt(
            request,
            public_key=public,
            key_id=key_id,
            bindings=bindings,
        )
        if started < int(backup_payload["finished_at_epoch"]):
            raise IsolationError("runtime_retirement_precedes_backup")
        purge_payload, purge_digest = _read_isolation_predecessor(
            "purge-legacy-runtime-exposure",
            request,
            bindings=bindings,
            public_key=public,
            key_id=key_id,
        )
        purge_result = purge_payload.get("result")
        if (
            started < int(purge_payload["finished_at_epoch"])
            or not isinstance(purge_result, dict)
            or purge_result.get("backup_receipt_sha256") != backup_digest
            or purge_result.get("pre_purge_root_env_digest")
            != bindings.get("pre_purge_root_env_digest")
            or purge_result.get("post_purge_root_env_digest")
            != bindings.get("post_purge_root_env_digest")
        ):
            raise IsolationError("runtime_retirement_purge_chain_invalid")
        result = _retire_stale_runtime(
            runtime_retirement,
            backup_receipt_sha256=backup_digest,
        )
        result["purge_receipt_sha256"] = purge_digest
    else:
        inputs = _validate_runtime_inputs(require_legacy_mail=False)
        if _current_runtime_inputs() != bindings.get("runtime_inputs"):
            raise IsolationError("post_purge_runtime_inputs_invalid")
        backup_payload, backup_digest = _read_backup_receipt(
            request,
            public_key=public,
            key_id=key_id,
            bindings=bindings,
        )
        exposure = _runtime_exposure(
            request,
            require_source_purged=True,
            cloudflared_image=str(bindings["cloudflared_image"]),
            database_image=str(bindings["database_image"]),
            api_host_ip=str(bindings["api_host_ip"]),
            api_host_port=int(bindings["api_host_port"]),
            api_container_port=int(bindings["api_container_port"]),
        )
        try:
            database_substrate = backup_contract._verify_database_substrate(  # noqa: SLF001
                request.database_image
            )
        except backup_contract.BackupError as exc:
            raise IsolationError("database_substrate_invalid", exc.code) from exc
        if database_substrate != bindings.get("database_substrate"):
            raise IsolationError("database_substrate_chain_invalid")
        purge_payload, purge_digest = _read_isolation_predecessor(
            "purge-legacy-runtime-exposure",
            request,
            bindings=bindings,
            public_key=public,
            key_id=key_id,
        )
        retirement_payload, retirement_digest = _read_isolation_predecessor(
            RETIREMENT_OPERATION,
            request,
            bindings=bindings,
            public_key=public,
            key_id=key_id,
        )
        purge_result = purge_payload.get("result")
        retirement_result = retirement_payload.get("result")
        if (
            not isinstance(purge_result, dict)
            or not isinstance(retirement_result, dict)
            or purge_result.get("backup_receipt_sha256") != backup_digest
            or retirement_result.get("backup_receipt_sha256") != backup_digest
            or retirement_result.get("purge_receipt_sha256") != purge_digest
            or int(retirement_payload["started_at_epoch"])
            < int(purge_payload["finished_at_epoch"])
        ):
            raise IsolationError("isolation_receipt_chain_invalid")
        database_receipts = _database_receipts(
            request,
            bindings=bindings,
            public_key=public,
            key_id=key_id,
            earliest_started_at=int(retirement_payload["finished_at_epoch"]),
            expected_substrate=database_substrate,
            backup_digest=backup_digest,
            purge_digest=purge_digest,
            retirement_digest=retirement_digest,
        )
        database_image_ids = {
            str(item["database_image_id"])
            for item in database_receipts.values()
            if isinstance(item, dict)
        }
        database_repo_digests = {
            str(item["database_repo_digest"])
            for item in database_receipts.values()
            if isinstance(item, dict)
        }
        exposure_containers = exposure.get("containers")
        database_exposure = [
            item
            for item in exposure_containers
            if isinstance(item, dict)
            and item.get("name") == "propertyquarry-db-live"
        ] if isinstance(exposure_containers, list) else []
        if (
            len(database_image_ids) != 1
            or len(database_repo_digests) != 1
            or len(database_exposure) != 1
            or database_exposure[0].get("image_id") not in database_image_ids
            or database_exposure[0].get("repo_digest") not in database_repo_digests
            or backup_payload.get("database_image_id") not in database_image_ids
            or backup_payload.get("database_repo_digest")
            not in database_repo_digests
        ):
            raise IsolationError("database_identity_chain_invalid")
        final_database_finished = max(
            int(item["finished_at_epoch"])
            for item in database_receipts.values()
            if isinstance(item, dict)
        )
        _deploy_payload, deploy_digest = _read_deploy_receipt(
            request,
            bindings=bindings,
            public_key=public,
            key_id=key_id,
            backup_digest=backup_digest,
            purge_digest=purge_digest,
            retirement_digest=retirement_digest,
            database_receipts=database_receipts,
            database_substrate=database_substrate,
            earliest_started_at=final_database_finished,
        )
        if started < int(_deploy_payload["finished_at_epoch"]):
            raise IsolationError("terminal_verification_precedes_deploy")
        result = {
            "backup_receipt_sha256": backup_digest,
            "database_substrate": database_substrate,
            "deploy_receipt_sha256": deploy_digest,
            "database_receipts": database_receipts,
            "exposure": exposure,
            "inputs": bindings["runtime_inputs"],
            "local_http": _local_http(host_port=int(bindings["api_host_port"])),
        }
    finished = int(time.time())
    payload = {
        **_receipt_binding_fields(bindings),
        "envelope_sha": request.envelope_sha,
        "finished_at_epoch": finished,
        "host_machine_id_digest": backup_contract._machine_id_digest(  # noqa: SLF001
            backup_contract.MACHINE_ID_PATH
        ),
        "operation": operation,
        "production_ready": False,
        "receipt_authority_key_id": key_id,
        "render_image": request.render_image,
        "result": result,
        "runtime_sha": request.runtime_sha,
        "schema": SCHEMA,
        "secret_values_emitted": False,
        "started_at_epoch": started,
        "status": "verified",
        "web_image": request.web_image,
    }
    wrapper = _sign_payload(payload, private_key=private, key_id=key_id)
    _write_receipt(receipt, wrapper)
    return wrapper


def _signed_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-sha", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--envelope-sha", required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--render-image", required=True)
    parser.add_argument("--cloudflared-image", required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--api-host-ip", required=True)
    parser.add_argument("--api-host-port", required=True, type=int)
    parser.add_argument("--api-container-port", required=True, type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("prepare-runtime-inputs")
    subparsers.add_parser("prepare-registration-email-input")
    verify_inputs = subparsers.add_parser("verify-isolation-inputs")
    _signed_arguments(verify_inputs)
    verify_inputs.add_argument("--pre-purge-root-env-digest", required=True)
    for operation in SIGNED_OPERATIONS:
        operation_parser = subparsers.add_parser(operation)
        _signed_arguments(operation_parser)
        operation_parser.add_argument("--receipt", required=True)
        if operation in {
            "purge-legacy-runtime-exposure",
            "restore-legacy-runtime-exposure",
        }:
            operation_parser.add_argument(
                "--pre-purge-root-env-digest",
                required=True,
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation in {
            "prepare-runtime-inputs",
            "prepare-registration-email-input",
        }:
            result = prepare_registration_email_input()
        elif args.operation == "verify-isolation-inputs":
            result = verify_isolation_inputs(args)
        else:
            wrapper = execute_signed(args)
            result = {
                "operation": wrapper["payload"]["operation"],
                "status": "ok",
            }
    except IsolationError as exc:
        print(
            json.dumps(
                {"detail": exc.detail, "reason": exc.code, "status": "failed"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
