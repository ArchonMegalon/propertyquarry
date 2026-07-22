from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from scripts import propertyquarry_database_control_v2 as control


RUNTIME_SHA = "a" * 64
WEB_IMAGE = f"ghcr.io/example/propertyquarry@sha256:{'b' * 64}"
MIGRATOR_URL = "postgresql://migrator:migrator-secret@propertyquarry-db/propertyquarry"
API_URL = "postgresql://api:api-secret@propertyquarry-db/propertyquarry"


def _runtime_values() -> dict[str, str]:
    return {
        "PROPERTYQUARRY_MIGRATION_DATABASE_URL": MIGRATOR_URL,
        "PROPERTYQUARRY_API_DATABASE_URL": API_URL,
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
        lambda _args: ("verify-schema-readiness", RUNTIME_SHA, WEB_IMAGE, receipt),
    )
    monkeypatch.setattr(
        control,
        "_load_authority",
        lambda **_kwargs: (
            {"receipt_authority_key_id": "sha256:key"},
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
    assert payload["runtime_sha"] == RUNTIME_SHA
    assert payload["web_image"] == WEB_IMAGE
    assert payload["started_at_epoch"] == 100
    assert payload["finished_at_epoch"] == 103
    assert payload["secret_values_emitted"] is False
    assert payload["production_ready"] is False


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
    receipt = root / RUNTIME_SHA / "migrate-schema.json"
    monkeypatch.setattr(control, "RECEIPT_ROOT", root)
    monkeypatch.setattr(control.os, "geteuid", lambda: 0)
    args = argparse.Namespace(
        operation="migrate-schema",
        runtime_sha=RUNTIME_SHA,
        web_image=WEB_IMAGE,
        receipt=str(receipt),
    )

    assert control._validate_request(args) == (
        "migrate-schema",
        RUNTIME_SHA,
        WEB_IMAGE,
        receipt,
    )

    args.web_image = "ghcr.io/example/propertyquarry:latest"
    with pytest.raises(control.DatabaseControlError, match="web_image_invalid"):
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
    assert "provision-roles" in completed.stdout
    assert "verify-schema-readiness" in completed.stdout
