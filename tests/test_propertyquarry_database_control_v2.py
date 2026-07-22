from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from scripts import propertyquarry_database_control_v2 as control


RUNTIME_SHA = "a" * 40
DEPLOYMENT_ID = "d" * 64
WEB_IMAGE = f"ghcr.io/example/propertyquarry@sha256:{'b' * 64}"
DATABASE_IMAGE = control.EXPECTED_DATABASE_IMAGE
DATABASE_IMAGE_ID = "sha256:" + "c" * 64
DATABASE_CONTAINER_ID = "e" * 64
MIGRATOR_URL = "postgresql://migrator:migrator-secret@propertyquarry-db/propertyquarry"
API_URL = "postgresql://api:api-secret@propertyquarry-db/propertyquarry"


def _runtime_values() -> dict[str, str]:
    return {
        "PROPERTYQUARRY_MIGRATION_DATABASE_URL": MIGRATOR_URL,
        "PROPERTYQUARRY_API_DATABASE_URL": API_URL,
    }


def _runtime_inputs() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index, path in enumerate(control.RUNTIME_INPUT_PATHS):
        raw = b"roles-env\n" if index == 2 else f"input-{index}\n".encode()
        result.append(
            {
                "gid": 1000,
                "mode": 0o600,
                "path": str(path),
                "sha256": control._sha256_id(raw),
                "size": len(raw),
                "uid": 1000,
            }
        )
    return result


def _pgdata_volume() -> dict[str, object]:
    return {
        "created_at": "2026-06-20T22:22:54+02:00",
        "driver": "local",
        "labels": {
            "com.docker.compose.config-hash": "f" * 64,
            "com.docker.compose.project": "property",
            "com.docker.compose.version": "5.1.3",
            "com.docker.compose.volume": "propertyquarry_pgdata",
        },
        "mountpoint": control.DATABASE_VOLUME_MOUNTPOINT,
        "name": control.DATABASE_VOLUME,
        "options": {},
        "scope": "local",
    }


def _database_substrate(*, database_oid: int = 83) -> dict[str, object]:
    return {
        "container_id": DATABASE_CONTAINER_ID,
        "container_name": control.DATABASE_CONTAINER,
        "database": control.runtime_database.TARGET_DATABASE,
        "database_oid": database_oid,
        "image": DATABASE_IMAGE,
        "image_id": DATABASE_IMAGE_ID,
        "pgdata_volume": _pgdata_volume(),
        "repo_digest": control._canonical_repo_digest(DATABASE_IMAGE),
    }


def _authority() -> dict[str, object]:
    inputs = _runtime_inputs()
    return {
        "backup_max_age_seconds": 3600,
        "database_image": DATABASE_IMAGE,
        "database_substrate": _database_substrate(),
        "deployment_id": DEPLOYMENT_ID,
        "post_purge_root_env_digest": inputs[0]["sha256"],
        "pre_purge_root_env_digest": inputs[0]["sha256"],
        "pre_purge_runtime_inputs": copy.deepcopy(inputs),
        "receipt_authority_key_id": "sha256:key",
        "runtime_inputs": inputs,
        "runtime_sha": RUNTIME_SHA,
        "schema": "propertyquarry.release-control.single-host-profile.v2",
        "transaction_started_at_epoch": 1,
        "web_image": WEB_IMAGE,
    }


def _signed_receipt_bytes(
    payload: dict[str, object],
    *,
    private_key: Ed25519PrivateKey,
    signature_domain: bytes,
) -> bytes:
    encoded = control._canonical_json(payload)
    signature = private_key.sign(
        signature_domain
        + len(encoded).to_bytes(8, byteorder="big", signed=False)
        + encoded
    )
    wrapper = {
        "payload": payload,
        "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        "signature_key_id": "sha256:key",
    }
    return control._canonical_json(wrapper) + b"\n"


def _external_predecessor_payloads() -> dict[str, tuple[dict[str, object], str]]:
    authority = _authority()
    common = {
        "authority_digest": "sha256:authority",
        "backup_max_age_seconds": 3600,
        "deployment_id": DEPLOYMENT_ID,
        "host_machine_id_digest": "sha256:machine",
        "production_ready": False,
        "receipt_authority_key_id": "sha256:key",
        "runtime_sha": RUNTIME_SHA,
        "secret_values_emitted": False,
        "status": "verified",
        "transaction_started_at_epoch": 1,
    }
    backup_digest = "sha256:" + "1" * 64
    backup = {
        **common,
        "database_image": DATABASE_IMAGE,
        "database_image_id": DATABASE_IMAGE_ID,
        "database_repo_digest": control._canonical_repo_digest(DATABASE_IMAGE),
        "database_substrate_after": _database_substrate(),
        "database_substrate_before": _database_substrate(),
        "finished_at_epoch": 11,
        "pre_purge_runtime_inputs": copy.deepcopy(
            authority["pre_purge_runtime_inputs"]
        ),
        "schema": control.BACKUP_RECEIPT_SCHEMA,
        "started_at_epoch": 10,
    }
    purge = {
        **common,
        "finished_at_epoch": 13,
        "operation": "purge-legacy-runtime-exposure",
        "post_purge_root_env_digest": authority[
            "post_purge_root_env_digest"
        ],
        "pre_purge_root_env_digest": authority[
            "pre_purge_root_env_digest"
        ],
        "pre_purge_runtime_inputs": copy.deepcopy(
            authority["pre_purge_runtime_inputs"]
        ),
        "result": {
            "backup_receipt_sha256": backup_digest,
            "inputs": {
                "file_digests": {
                    str(item["path"]): str(item["sha256"])
                    for item in authority["runtime_inputs"]
                },
                "google_key_count": 5,
                "legacy_registration_email_present": False,
                "registration_email_key_count": 8,
            },
            "post_purge_root_env_digest": authority[
                "post_purge_root_env_digest"
            ],
            "pre_purge_root_env_digest": authority[
                "pre_purge_root_env_digest"
            ],
            "legacy_keys_removed": 8,
            "rollback_artifact": {"sha256": "sha256:" + "9" * 64},
            "rollback_artifact_expected_removed_keys": 8,
        },
        "runtime_inputs": copy.deepcopy(authority["runtime_inputs"]),
        "schema": control.ISOLATION_RECEIPT_SCHEMA,
        "started_at_epoch": 12,
    }
    retirement = {
        **common,
        "finished_at_epoch": 15,
        "operation": "retire-stale-propertyquarry-runtime",
        "result": {
            "backup_receipt_sha256": backup_digest,
            "purge_receipt_sha256": "sha256:" + "2" * 64,
        },
        "schema": control.ISOLATION_RECEIPT_SCHEMA,
        "started_at_epoch": 14,
    }
    return {
        "create.json": (backup, backup_digest),
        "purge-legacy-runtime-exposure.json": (
            purge,
            "sha256:" + "2" * 64,
        ),
        "retire-stale-propertyquarry-runtime.json": (
            retirement,
            "sha256:" + "3" * 64,
        ),
    }


def _prior_database_payload(
    *,
    operation: str,
    predecessor_digest: str,
    started_at: int,
    finished_at: int,
) -> dict[str, object]:
    inputs = _runtime_inputs()
    return {
        "authority_digest": "sha256:authority",
        "backup_max_age_seconds": 3600,
        "backup_receipt_sha256": "sha256:" + "1" * 64,
        "database": control.runtime_database.TARGET_DATABASE,
        "database_container": control.DATABASE_CONTAINER,
        "database_image": DATABASE_IMAGE,
        "database_image_id": DATABASE_IMAGE_ID,
        "database_repo_digest": control._canonical_repo_digest(DATABASE_IMAGE),
        "database_substrate_after": _database_substrate(),
        "database_substrate_before": _database_substrate(),
        "deployment_id": DEPLOYMENT_ID,
        "docker_network": control.DOCKER_NETWORK,
        "env_file": str(control.RUNTIME_ENV_FILE),
        "env_file_sha256": inputs[2]["sha256"],
        "finished_at_epoch": finished_at,
        "host_machine_id_digest": "sha256:machine",
        "operation": operation,
        "predecessor_receipt_sha256": predecessor_digest,
        "production_ready": False,
        "purge_receipt_sha256": "sha256:" + "2" * 64,
        "receipt_authority_key_id": "sha256:key",
        "result": {"database_oid": 83},
        "retirement_receipt_sha256": "sha256:" + "3" * 64,
        "runtime_inputs": inputs,
        "runtime_sha": RUNTIME_SHA,
        "schema": control.RECEIPT_SCHEMA,
        "secret_values_emitted": False,
        "started_at_epoch": started_at,
        "status": "verified",
        "transaction_started_at_epoch": 1,
        "web_image": WEB_IMAGE,
    }


def test_schema_container_is_pinned_hardened_and_keeps_secret_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"ready": True, "status": "ready"}) + "\n",
            "",
        )

    monkeypatch.setattr(control.subprocess, "run", fake_run)

    result = control._run_schema_container(
        operation="check",
        database_url=API_URL,
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        hidden=(API_URL, "api-secret"),
    )

    argv = tuple(observed["argv"])
    kwargs = dict(observed["kwargs"])
    assert result == {"ready": True, "status": "ready"}
    assert argv[0:3] == ("/usr/bin/docker", "run", "--rm")
    for required in (
        "--pull=never",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--network",
    ):
        assert required in argv
    assert argv[argv.index("--network") + 1] == control.DOCKER_NETWORK
    assert WEB_IMAGE in argv
    assert API_URL not in " ".join(argv)
    assert "api-secret" not in " ".join(argv)
    assert kwargs["env"] == {
        "DATABASE_URL": API_URL,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": RUNTIME_SHA,
        "TZ": "UTC",
    }
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_database_gate_measures_exact_live_database_substrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = {
        "Id": DATABASE_CONTAINER_ID,
        "Name": f"/{control.DATABASE_CONTAINER}",
        "Config": {
            "Cmd": ["postgres"],
            "Entrypoint": ["docker-entrypoint.sh"],
            "Healthcheck": {
                "Interval": 30_000_000_000,
                "Retries": 3,
                "Test": [
                    "CMD-SHELL",
                    "pg_isready -U postgres -d propertyquarry",
                ],
                "Timeout": 5_000_000_000,
            },
            "Image": DATABASE_IMAGE,
            "Labels": {
                "com.docker.compose.project": "property",
                "com.docker.compose.service": "propertyquarry-db",
            },
            "User": "",
        },
        "HostConfig": {
            "CapAdd": None,
            "CapDrop": None,
            "CgroupParent": "system.slice",
            "Init": True,
            "PidsLimit": 128,
            "PortBindings": {},
            "Privileged": False,
            "PublishAllPorts": False,
            "ReadonlyRootfs": False,
            "RestartPolicy": {"MaximumRetryCount": 3, "Name": "on-failure"},
            "SecurityOpt": None,
        },
        "Image": DATABASE_IMAGE_ID,
        "Mounts": [
            {
                "Destination": "/var/lib/postgresql/data",
                "Driver": "local",
                "Mode": "rw",
                "Name": control.DATABASE_VOLUME,
                "Propagation": "",
                "RW": True,
                "Source": control.DATABASE_VOLUME_MOUNTPOINT,
                "Type": "volume",
            }
        ],
        "NetworkSettings": {
            "Networks": {
                "property_default": {},
                "property_propertyquarry_render_internal": {},
            },
            "Ports": {"5432/tcp": None},
        },
        "State": {"Health": {"Status": "healthy"}, "Status": "running"},
    }
    image = {
        "Id": DATABASE_IMAGE_ID,
        "RepoDigests": [control._canonical_repo_digest(DATABASE_IMAGE)],
    }
    volume = {
        "CreatedAt": _pgdata_volume()["created_at"],
        "Driver": "local",
        "Labels": _pgdata_volume()["labels"],
        "Mountpoint": control.DATABASE_VOLUME_MOUNTPOINT,
        "Name": control.DATABASE_VOLUME,
        "Options": None,
        "Scope": "local",
    }
    monkeypatch.setattr(
        control,
        "_docker_inspect",
        lambda kind, _target: {
            "container": container,
            "image": image,
            "volume": volume,
        }[kind],
    )
    monkeypatch.setattr(
        control.runtime_database,
        "_database_oids",
        lambda **_kwargs: (None, 83),
    )

    assert control._verify_database_container_image(
        DATABASE_IMAGE
    ) == _database_substrate()

    container["HostConfig"]["PortBindings"] = {
        "5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]
    }
    with pytest.raises(
        control.DatabaseControlError,
        match="database_container_image_identity_invalid",
    ):
        control._verify_database_container_image(DATABASE_IMAGE)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(container_id="f" * 63),
        lambda value: value.update(database_oid=True),
        lambda value: value["pgdata_volume"].update(mountpoint="/tmp/pgdata"),
        lambda value: value["pgdata_volume"].update(options={"uid": 1000}),
    ),
)
def test_database_substrate_rejects_container_volume_and_oid_tampering(
    mutation,
) -> None:  # type: ignore[no-untyped-def]
    substrate = _database_substrate()
    mutation(substrate)

    with pytest.raises(control.DatabaseControlError):
        control._validate_database_substrate(substrate)


def test_runtime_inputs_require_exact_six_file_order_and_metadata() -> None:
    inputs = _runtime_inputs()
    assert control._validate_runtime_inputs(inputs) == inputs

    reordered = copy.deepcopy(inputs)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    with pytest.raises(control.DatabaseControlError, match="runtime_inputs_invalid"):
        control._validate_runtime_inputs(reordered)

    boolean_size = copy.deepcopy(inputs)
    boolean_size[0]["size"] = True
    with pytest.raises(control.DatabaseControlError, match="runtime_inputs_invalid"):
        control._validate_runtime_inputs(boolean_size)


def test_authority_requires_identical_signed_plan_runtime_and_substrate_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority()
    common_fields = (
        "backup_max_age_seconds",
        "database_image",
        "database_substrate",
        "deployment_id",
        "post_purge_root_env_digest",
        "pre_purge_root_env_digest",
        "pre_purge_runtime_inputs",
        "runtime_inputs",
        "runtime_sha",
        "transaction_started_at_epoch",
        "web_image",
    )
    plan = {
        "schema": "propertyquarry.release-control.single-host-transaction-plan.v2",
        **{field: copy.deepcopy(authority[field]) for field in common_fields},
    }
    plan_raw = control._canonical_json(plan)
    authority["plan_digest"] = control._sha256_id(plan_raw)
    authority_raw = control._canonical_json(authority)
    documents = {
        control.AUTHORITY_PATH: authority_raw,
        control.TRANSACTION_PLAN_PATH: plan_raw,
    }
    monkeypatch.setattr(
        control,
        "_read_regular",
        lambda path, **_kwargs: documents[path],
    )

    loaded, digest = control._load_authority(
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        web_image=WEB_IMAGE,
        database_image=DATABASE_IMAGE,
    )
    assert loaded == authority
    assert digest == control._sha256_id(authority_raw)

    plan["database_substrate"] = _database_substrate(database_oid=84)
    changed_plan = control._canonical_json(plan)
    authority["plan_digest"] = control._sha256_id(changed_plan)
    documents[control.AUTHORITY_PATH] = control._canonical_json(authority)
    documents[control.TRANSACTION_PLAN_PATH] = changed_plan
    with pytest.raises(
        control.DatabaseControlError,
        match="authority_plan_binding_invalid",
    ):
        control._load_authority(
            runtime_sha=RUNTIME_SHA,
            deployment_id=DEPLOYMENT_ID,
            web_image=WEB_IMAGE,
            database_image=DATABASE_IMAGE,
        )


def test_signed_predecessor_receipt_is_canonical_and_domain_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    payload = {"deployment_id": DEPLOYMENT_ID, "runtime_sha": RUNTIME_SHA}
    raw = _signed_receipt_bytes(
        payload,
        private_key=private_key,
        signature_domain=control.BACKUP_RECEIPT_SIGNATURE_DOMAIN,
    )
    monkeypatch.setattr(control, "_read_regular", lambda *_args, **_kwargs: raw)

    loaded, digest = control._read_signed_receipt(
        Path("/receipt.json"),
        signature_domain=control.BACKUP_RECEIPT_SIGNATURE_DOMAIN,
        public_key=private_key.public_key(),
        key_id="sha256:key",
    )
    assert loaded == payload
    assert digest == control._sha256_id(raw)

    with pytest.raises(
        control.DatabaseControlError,
        match="predecessor_receipt_signature_invalid",
    ):
        control._read_signed_receipt(
            Path("/receipt.json"),
            signature_domain=control.ISOLATION_RECEIPT_SIGNATURE_DOMAIN,
            public_key=private_key.public_key(),
            key_id="sha256:key",
        )


def test_predecessor_chain_binds_deployment_order_and_immediate_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = _external_predecessor_payloads()
    provision_digest = "sha256:" + "4" * 64
    receipts["provision-roles.json"] = (
        _prior_database_payload(
            operation="provision-roles",
            predecessor_digest="sha256:" + "3" * 64,
            started_at=16,
            finished_at=17,
        ),
        provision_digest,
    )
    monkeypatch.setattr(
        control,
        "_read_signed_receipt",
        lambda path, **_kwargs: copy.deepcopy(receipts[path.name]),
    )

    bindings = control._load_predecessor_chain(
        operation="migrate-schema",
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        web_image=WEB_IMAGE,
        database_image=DATABASE_IMAGE,
        authority=_authority(),
        authority_digest="sha256:authority",
        public_key=object(),
        receipt_key_id="sha256:key",
        host_machine_id_digest="sha256:machine",
        gate_started_at=20,
    )

    assert bindings == {
        "backup_receipt_sha256": "sha256:" + "1" * 64,
        "predecessor_finished_at_epoch": 17,
        "predecessor_receipt_sha256": provision_digest,
        "purge_receipt_sha256": "sha256:" + "2" * 64,
        "retirement_receipt_sha256": "sha256:" + "3" * 64,
    }

    receipts["provision-roles.json"][0]["database_substrate_after"][
        "container_id"
    ] = "f" * 64
    with pytest.raises(
        control.DatabaseControlError,
        match="predecessor_database_receipt_binding_invalid",
    ):
        control._load_predecessor_chain(
            operation="migrate-schema",
            runtime_sha=RUNTIME_SHA,
            deployment_id=DEPLOYMENT_ID,
            web_image=WEB_IMAGE,
            database_image=DATABASE_IMAGE,
            authority=_authority(),
            authority_digest="sha256:authority",
            public_key=object(),
            receipt_key_id="sha256:key",
            host_machine_id_digest="sha256:machine",
            gate_started_at=20,
        )


def test_predecessor_chain_rejects_backup_outside_signed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = _external_predecessor_payloads()
    monkeypatch.setattr(
        control,
        "_read_signed_receipt",
        lambda path, **_kwargs: copy.deepcopy(receipts[path.name]),
    )

    with pytest.raises(
        control.DatabaseControlError,
        match="predecessor_receipt_order_invalid",
    ):
        control._load_predecessor_chain(
            operation="provision-roles",
            runtime_sha=RUNTIME_SHA,
            deployment_id=DEPLOYMENT_ID,
            web_image=WEB_IMAGE,
            database_image=DATABASE_IMAGE,
            authority=_authority(),
            authority_digest="sha256:authority",
            public_key=object(),
            receipt_key_id="sha256:key",
            host_machine_id_digest="sha256:machine",
            gate_started_at=3612,
        )


@pytest.mark.parametrize(
    ("operation", "schema_operation", "database_url", "acl_expected"),
    (
        ("migrate-schema", "migrate", MIGRATOR_URL, False),
        ("harden-runtime-acl", "check", MIGRATOR_URL, True),
        ("verify-schema-readiness", "check", API_URL, False),
    ),
)
def test_database_operations_use_the_exact_role_and_order(
    operation: str,
    schema_operation: str,
    database_url: str,
    acl_expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psql_calls: list[dict[str, object]] = []
    schema_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        control,
        "_load_values",
        lambda *, allow_create: (_runtime_values(), True, False),
    )
    monkeypatch.setattr(
        control,
        "_secret_context",
        lambda _values: ({}, (MIGRATOR_URL, API_URL)),
    )
    monkeypatch.setattr(control, "_database_target_guard", lambda **_kwargs: 71)
    monkeypatch.setattr(
        control.runtime_database,
        "_role_authority_guard_sql",
        lambda: "ROLE_AUTHORITY_GUARD",
    )
    monkeypatch.setattr(control.runtime_database, "_configure_sql", lambda: "ACL")

    def fake_psql(**kwargs: object) -> str:
        psql_calls.append(dict(kwargs))
        return ""

    def fake_schema(**kwargs: object) -> dict[str, object]:
        schema_calls.append(dict(kwargs))
        if kwargs["operation"] == "migrate":
            return {"status": "migrated"}
        return {"ready": True, "status": "ready"}

    monkeypatch.setattr(control.runtime_database, "_psql", fake_psql)
    monkeypatch.setattr(control, "_run_schema_container", fake_schema)

    result = control._operate(
        operation=operation,
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
    )

    assert result["database_oid"] == 71
    assert [(call["operation"], call["database_url"]) for call in schema_calls] == [
        (schema_operation, database_url)
    ]
    assert psql_calls[0]["database"] == "template1"
    assert psql_calls[0]["sql"] == "ROLE_AUTHORITY_GUARD"
    acl_calls = [call for call in psql_calls if call["sql"] == "ACL"]
    assert bool(acl_calls) is acl_expected
    if acl_expected:
        assert acl_calls[0]["database"] == control.runtime_database.TARGET_DATABASE
        assert schema_calls[0]["operation"] == "check"


def test_role_provisioning_defers_runtime_acl_until_after_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psql_calls: list[dict[str, object]] = []
    sentinel_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        control,
        "_load_values",
        lambda *, allow_create: (_runtime_values(), True, False),
    )
    monkeypatch.setattr(
        control,
        "_secret_context",
        lambda _values: ({"role": "secret"}, ("secret",)),
    )
    monkeypatch.setattr(
        control.runtime_database,
        "_prepare_roles_sql",
        lambda _passwords: "PREPARE_ROLES",
    )
    monkeypatch.setattr(
        control.runtime_database,
        "_database_oids",
        lambda **_kwargs: (None, 83),
    )
    monkeypatch.setattr(
        control.runtime_database,
        "_psql",
        lambda **kwargs: psql_calls.append(dict(kwargs)) or "",
    )
    monkeypatch.setattr(
        control.runtime_database,
        "_check_sentinel",
        lambda **kwargs: sentinel_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        control.runtime_database,
        "_configure_sql",
        lambda: pytest.fail("ACL hardening must be a separate post-migration gate"),
    )
    monkeypatch.setattr(control, "_validate_env_file", lambda _path: b"env")

    result = control._provision_roles()

    assert result["database_oid"] == 83
    assert [call["sql"] for call in psql_calls] == ["PREPARE_ROLES"]
    assert sentinel_calls == [
        {
            "container": control.DATABASE_CONTAINER,
            "database": control.runtime_database.TARGET_DATABASE,
            "database_oid": 83,
            "hidden": ("secret",),
            "install": True,
        }
    ]


def test_receipt_signature_is_domain_separated_and_canonical() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    payload = {"operation": "migrate-schema", "runtime_sha": RUNTIME_SHA}

    wrapper = control._sign_payload(
        payload,
        private_key=private_key,
        key_id="sha256:key",
    )

    encoded = control._canonical_json(payload)
    signature_text = str(wrapper["signature"])
    signature = base64.urlsafe_b64decode(
        signature_text + "=" * ((4 - len(signature_text) % 4) % 4)
    )
    private_key.public_key().verify(
        signature,
        control.RECEIPT_SIGNATURE_DOMAIN
        + len(encoded).to_bytes(8, byteorder="big", signed=False)
        + encoded,
    )
    assert wrapper == {
        "payload": payload,
        "signature": signature_text,
        "signature_key_id": "sha256:key",
    }


def test_execute_binds_authority_runtime_image_credentials_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "receipt.json"
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    written: dict[str, object] = {}
    times = iter((100, 103))
    monkeypatch.setattr(
        control,
        "_validate_request",
        lambda _args: (
            "verify-schema-readiness",
            RUNTIME_SHA,
            DEPLOYMENT_ID,
            WEB_IMAGE,
            DATABASE_IMAGE,
            receipt,
        ),
    )
    monkeypatch.setattr(
        control,
        "_load_authority",
        lambda **_kwargs: (
            _authority(),
            "sha256:authority",
        ),
    )
    monkeypatch.setattr(
        control.backup_contract,
        "_load_receipt_authority",
        lambda *_args, **_kwargs: (private_key, private_key.public_key(), "sha256:key"),
    )
    monkeypatch.setattr(
        control,
        "_operate",
        lambda **_kwargs: {"database_oid": 83, "schema": {"ready": True}},
    )
    monkeypatch.setattr(
        control,
        "_verify_database_container_image",
        lambda _image: copy.deepcopy(_database_substrate()),
    )
    monkeypatch.setattr(
        control,
        "_load_predecessor_chain",
        lambda **_kwargs: {
            "backup_receipt_sha256": "sha256:" + "1" * 64,
            "predecessor_finished_at_epoch": 99,
            "predecessor_receipt_sha256": "sha256:" + "4" * 64,
            "purge_receipt_sha256": "sha256:" + "2" * 64,
            "retirement_receipt_sha256": "sha256:" + "3" * 64,
        },
    )
    monkeypatch.setattr(
        control,
        "_measure_runtime_inputs",
        lambda: copy.deepcopy(_runtime_inputs()),
    )
    monkeypatch.setattr(control, "_validate_env_file", lambda _path: b"roles-env\n")
    monkeypatch.setattr(
        control.backup_contract,
        "_machine_id_digest",
        lambda _path: "sha256:machine",
    )
    monkeypatch.setattr(control.time, "time", lambda: next(times))
    monkeypatch.setattr(
        control,
        "_write_receipt",
        lambda path, wrapper: written.update(path=path, wrapper=wrapper),
    )

    wrapper = control.execute(argparse.Namespace())

    payload = wrapper["payload"]
    assert written == {"path": receipt, "wrapper": wrapper}
    assert payload["authority_digest"] == "sha256:authority"
    assert payload["env_file_sha256"] == control._sha256_id(b"roles-env\n")
    assert payload["host_machine_id_digest"] == "sha256:machine"
    assert payload["operation"] == "verify-schema-readiness"
    assert payload["database_image"] == DATABASE_IMAGE
    assert payload["database_image_id"] == DATABASE_IMAGE_ID
    assert payload["database_repo_digest"] == control._canonical_repo_digest(
        DATABASE_IMAGE
    )
    assert payload["runtime_sha"] == RUNTIME_SHA
    assert payload["deployment_id"] == DEPLOYMENT_ID
    assert payload["runtime_inputs"] == _runtime_inputs()
    assert payload["database_substrate_before"] == _database_substrate()
    assert payload["database_substrate_after"] == _database_substrate()
    assert payload["backup_receipt_sha256"] == "sha256:" + "1" * 64
    assert payload["purge_receipt_sha256"] == "sha256:" + "2" * 64
    assert payload["retirement_receipt_sha256"] == "sha256:" + "3" * 64
    assert payload["predecessor_receipt_sha256"] == "sha256:" + "4" * 64
    assert payload["transaction_started_at_epoch"] == 1
    assert payload["backup_max_age_seconds"] == 3600
    assert set(payload) == control.DATABASE_RECEIPT_PAYLOAD_KEYS
    assert payload["web_image"] == WEB_IMAGE
    assert payload["started_at_epoch"] == 100
    assert payload["finished_at_epoch"] == 103
    assert payload["secret_values_emitted"] is False
    assert payload["production_ready"] is False


def test_execute_rejects_database_oid_change_during_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    before = _database_substrate()
    after = _database_substrate(database_oid=84)
    observations = iter((before, after))
    monkeypatch.setattr(
        control,
        "_validate_request",
        lambda _args: (
            "verify-schema-readiness",
            RUNTIME_SHA,
            DEPLOYMENT_ID,
            WEB_IMAGE,
            DATABASE_IMAGE,
            tmp_path / "receipt.json",
        ),
    )
    monkeypatch.setattr(
        control,
        "_load_authority",
        lambda **_kwargs: (_authority(), "sha256:authority"),
    )
    monkeypatch.setattr(
        control.backup_contract,
        "_load_receipt_authority",
        lambda *_args, **_kwargs: (
            private_key,
            private_key.public_key(),
            "sha256:key",
        ),
    )
    monkeypatch.setattr(
        control.backup_contract,
        "_machine_id_digest",
        lambda _path: "sha256:machine",
    )
    monkeypatch.setattr(
        control,
        "_load_predecessor_chain",
        lambda **_kwargs: {
            "backup_receipt_sha256": "sha256:" + "1" * 64,
            "predecessor_finished_at_epoch": 99,
            "predecessor_receipt_sha256": "sha256:" + "4" * 64,
            "purge_receipt_sha256": "sha256:" + "2" * 64,
            "retirement_receipt_sha256": "sha256:" + "3" * 64,
        },
    )
    monkeypatch.setattr(
        control,
        "_measure_runtime_inputs",
        lambda: copy.deepcopy(_runtime_inputs()),
    )
    monkeypatch.setattr(
        control,
        "_verify_database_container_image",
        lambda _image: copy.deepcopy(next(observations)),
    )
    monkeypatch.setattr(
        control,
        "_operate",
        lambda **_kwargs: {"database_oid": 83, "schema": {"ready": True}},
    )
    monkeypatch.setattr(control.time, "time", lambda: 100)

    with pytest.raises(
        control.DatabaseControlError,
        match="database_container_identity_changed",
    ):
        control.execute(argparse.Namespace())


def test_failed_receipt_write_removes_the_private_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("receipt ownership contract is root-only")
    parent = tmp_path / "receipts"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(
        control.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("simulated fsync failure")),
    )

    with pytest.raises(OSError, match="simulated fsync failure"):
        control._write_receipt(parent / "gate.json", {"payload": {}})

    assert list(parent.iterdir()) == []


def test_request_requires_exact_operation_pinned_image_and_receipt_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "database-receipts"
    receipt = root / RUNTIME_SHA / DEPLOYMENT_ID / "migrate-schema.json"
    monkeypatch.setattr(control, "RECEIPT_ROOT", root)
    monkeypatch.setattr(control.os, "geteuid", lambda: 0)
    args = argparse.Namespace(
        operation="migrate-schema",
        runtime_sha=RUNTIME_SHA,
        deployment_id=DEPLOYMENT_ID,
        web_image=WEB_IMAGE,
        database_image=DATABASE_IMAGE,
        receipt=str(receipt),
    )

    assert control._validate_request(args) == (
        "migrate-schema",
        RUNTIME_SHA,
        DEPLOYMENT_ID,
        WEB_IMAGE,
        DATABASE_IMAGE,
        receipt,
    )

    args.runtime_sha = "a" * 64
    with pytest.raises(control.DatabaseControlError, match="runtime_sha_invalid"):
        control._validate_request(args)

    args.runtime_sha = RUNTIME_SHA
    args.deployment_id = "d" * 63
    with pytest.raises(control.DatabaseControlError, match="deployment_id_invalid"):
        control._validate_request(args)

    args.deployment_id = DEPLOYMENT_ID
    args.web_image = "ghcr.io/example/propertyquarry:latest"
    with pytest.raises(control.DatabaseControlError, match="web_image_invalid"):
        control._validate_request(args)

    args.web_image = WEB_IMAGE
    args.database_image = "postgres:latest"
    with pytest.raises(control.DatabaseControlError, match="database_image_invalid"):
        control._validate_request(args)


def test_installed_layout_loads_the_signed_hyphenated_backup_executable(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "libexec"
    install_root.mkdir()
    database_executable = install_root / "propertyquarry-database-control-v2"
    backup_executable = install_root / "propertyquarry-predeploy-backup-v2"
    runtime_module = install_root / "provision_propertyquarry_runtime_database.py"
    shutil.copyfile(Path(control.__file__), database_executable)
    shutil.copyfile(Path(control.backup_contract.__file__), backup_executable)
    shutil.copyfile(Path(control.runtime_database.__file__), runtime_module)

    completed = subprocess.run(
        [sys.executable, str(database_executable), "--help"],
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
    assert "--deployment-id" in completed.stdout
    assert "provision-roles" in completed.stdout
    assert "verify-schema-readiness" in completed.stdout
