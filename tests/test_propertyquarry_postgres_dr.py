from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import propertyquarry_postgres_dr as dr


RELEASE_COMMIT_SHA = "a" * 40
RELEASE_IMAGE_DIGEST = "sha256:" + "b" * 64
AWS_CLI_APPROVED_VERSION = "2.27.49"
AWS_CLI_APPROVED_SHA256 = "c" * 64
AWS_CLI_APPROVED_PATH = "/usr/local/bin/aws"
_REAL_LOAD_AWS_CLI_RELEASE_PIN = dr._load_aws_cli_release_pin


def _aws_cli_release_pin(
    *,
    path: str = AWS_CLI_APPROVED_PATH,
    version: str = AWS_CLI_APPROVED_VERSION,
    sha256: str = AWS_CLI_APPROVED_SHA256,
) -> dict[str, str]:
    raw = json.dumps(
        {
            "path": path,
            "schema": dr.AWS_CLI_RELEASE_PIN_SCHEMA,
            "sha256": sha256,
            "status": dr.AWS_CLI_RELEASE_PIN_CONFIGURED,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return dr._parse_aws_cli_release_pin(raw)


@pytest.fixture(autouse=True)
def _configured_release_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dr, "_load_aws_cli_release_pin", lambda: _aws_cli_release_pin())


def _release_env() -> dict[str, str]:
    return {
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": RELEASE_COMMIT_SHA,
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST": RELEASE_IMAGE_DIGEST,
    }


def _release_receipt() -> dict[str, str]:
    return {
        "git_commit_sha": RELEASE_COMMIT_SHA,
        "image_digest": RELEASE_IMAGE_DIGEST,
    }


def _aws_cli_attestation() -> dict[str, object]:
    return {
        "contract_name": dr.AWS_CLI_ATTESTATION_CONTRACT_NAME,
        "contract_version": dr.AWS_CLI_ATTESTATION_CONTRACT_VERSION,
        "path": AWS_CLI_APPROVED_PATH,
        "version": AWS_CLI_APPROVED_VERSION,
        "sha256": AWS_CLI_APPROVED_SHA256,
        "size_bytes": 12_345,
        "owner_uid": os.geteuid(),
        "mode": "0755",
        "device": 1,
        "inode": 2,
        "mtime_ns": 3,
        "regular_file": True,
        "symlink": False,
        "group_world_writable": False,
        "single_link": True,
        "minimal_path": dr.AWS_CLI_MINIMAL_PATH,
        "release_pin": _aws_cli_release_pin(),
    }


def _install_fake_aws_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, str]:
    cli = tmp_path / "aws"
    cli.write_bytes(b"propertyquarry-approved-test-aws-cli\n")
    cli.chmod(0o755)
    pin = _aws_cli_release_pin(
        path=str(cli),
        sha256=hashlib.sha256(cli.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(dr, "_load_aws_cli_release_pin", lambda: dict(pin))
    return pin


def _trusted_executable(tmp_path: Path, name: str) -> str:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return str(executable)


def _schema_ledger() -> dict[str, object]:
    return dr._expected_schema_ledger()


def _schema_ledger_prefix(version: int, *, ledger_present: bool = True) -> dict[str, object]:
    expected = _schema_ledger()
    payload: dict[str, object] = {
        "component": expected["component"],
        "ledger_table": expected["ledger_table"],
        "ledger_present": ledger_present,
        "current_version": version,
        "migrations": list(expected["migrations"])[:version],
    }
    payload["fingerprint_sha256"] = dr._schema_ledger_fingerprint(payload)
    return payload


def _critical_data_evidence(
    *,
    row_counts: dict[str, int] | None = None,
    fingerprint_salt: str = "release",
) -> dict[str, object]:
    counts = {table: 0 for table, _identity, _required in dr.CRITICAL_DATA_TABLES}
    counts["property_search_runs"] = 3
    counts.update(row_counts or {})
    contract = dr._critical_data_contract()
    tables: list[dict[str, object]] = []
    for table_contract in contract["tables"]:
        table = str(table_contract["table"])
        row_count = counts[table]
        chunks: list[dict[str, object]] = []
        remaining = row_count
        chunk_index = 0
        while remaining:
            chunk_rows = min(remaining, dr.CRITICAL_DATA_CHUNK_SIZE)
            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "row_count": chunk_rows,
                    "max_row_bytes_observed": 512,
                    "chunk_sha256": hashlib.sha256(
                        f"{table}:{chunk_index}:{chunk_rows}:{fingerprint_salt}".encode("utf-8")
                    ).hexdigest(),
                }
            )
            remaining -= chunk_rows
            chunk_index += 1
        merkle_root = dr._critical_merkle_root(table, chunks)
        tables.append(
            {
                **table_contract,
                "evidence_version": dr.CRITICAL_DATA_EVIDENCE_VERSION,
                "chunk_size": dr.CRITICAL_DATA_CHUNK_SIZE,
                "max_row_bytes": dr.CRITICAL_DATA_MAX_ROW_BYTES,
                "max_chunks": dr.CRITICAL_DATA_MAX_CHUNKS,
                "max_supported_rows": dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS,
                "row_count": row_count,
                "chunk_count": len(chunks),
                "chunks": chunks,
                "merkle_root_sha256": merkle_root,
                "fingerprint_sha256": merkle_root,
            }
        )
    return {
        **contract,
        "tables": tables,
    }


def _critical_query_result(sql: str, *, fingerprint_salt: str = "release") -> str:
    table = next(
        table
        for table, _identity, _required in dr.CRITICAL_DATA_TABLES
        if f"propertyquarry_critical_data:{table}" in sql
    )
    evidence = _critical_data_evidence(fingerprint_salt=fingerprint_salt)
    row = next(item for item in evidence["tables"] if item["table"] == table)
    if f"propertyquarry_critical_data:{table}:row_bound_preflight" in sql:
        return f'{row["row_count"]}\n'
    return json.dumps(
        {
            "evidence_version": row["evidence_version"],
            "row_count": row["row_count"],
            "oversized_row_count": 0,
            "chunk_count": row["chunk_count"],
            "chunks": row["chunks"],
        }
    ) + "\n"


def _source_snapshot_evidence(*, plaintext_sha256: str) -> dict[str, object]:
    return dr._snapshot_evidence(
        {
            "snapshot_id_sha256": hashlib.sha256(b"00000003-0000001B-1").hexdigest(),
            "transaction_snapshot_sha256": hashlib.sha256(b"100:200:150").hexdigest(),
        },
        pg_dump_plaintext_sha256=plaintext_sha256,
    )


class _SnapshotCursor:
    def __init__(self, connection: "_SnapshotConnection") -> None:
        self.connection = connection

    def execute(self, sql: str) -> None:
        self.connection.events.append(("snapshot_sql", sql))
        if sql.startswith("BEGIN TRANSACTION"):
            self.connection.active = True

    def fetchone(self) -> tuple[str, str]:
        assert self.connection.active is True
        return ("00000003-0000001B-1", "100:200:150")

    def close(self) -> None:
        self.connection.events.append(("snapshot_cursor_closed", ""))


class _SnapshotConnection:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.active = False
        self.closed = False

    def cursor(self) -> _SnapshotCursor:
        return _SnapshotCursor(self)

    def rollback(self) -> None:
        self.events.append(("snapshot_rollback", ""))
        self.active = False

    def close(self) -> None:
        self.events.append(("snapshot_connection_closed", ""))
        self.closed = True


class _SnapshotConnector:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.connection = _SnapshotConnection(self.events)

    def __call__(self, database_url: str, **kwargs: object) -> _SnapshotConnection:
        assert database_url.startswith("postgresql://")
        assert kwargs == {"autocommit": False, "connect_timeout": 5}
        self.events.append(("snapshot_connect", ""))
        return self.connection


def _off_host_object(
    *,
    artifact: Path,
    verified_at: str = "2026-07-13T10:00:01Z",
) -> dict[str, object]:
    return {
        "provider": "s3",
        "backend": "aws_s3api",
        "region": "eu-central-1",
        "bucket": "propertyquarry-dr-eu",
        "object_key": "prod/propertyquarry.dump.gpg",
        "version_id": "3LgX-release-object-version",
        "etag": "d41d8cd98f00b204e9800998ecf8427e",
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "size_bytes": artifact.stat().st_size,
        "encrypted": True,
        "off_host": True,
        "object_exists": True,
        "checksum_verified": True,
        "provider_request_id": "verify-request-release-1",
        "verified_at": verified_at,
        "verification_method": dr.REMOTE_PROVIDER_CONTRACTS["s3"]["verification_method"],
    }


def _off_host_retrieval(
    *,
    artifact: Path,
    retrieved_at: str = "2026-07-13T10:00:10Z",
) -> dict[str, object]:
    remote = _off_host_object(artifact=artifact)
    return {
        "schema": dr.OFF_HOST_RETRIEVAL_SCHEMA,
        **{
            key: remote[key]
            for key in (
                "provider",
                "backend",
                "region",
                "bucket",
                "object_key",
                "version_id",
                "etag",
            )
        },
        "sha256": remote["sha256"],
        "size_bytes": remote["size_bytes"],
        "object_exists": True,
        "provider_verified": True,
        "version_verified": True,
        "checksum_verified": True,
        "provider_request_id": "provider-request-release-1",
        "retrieval_method": dr.REMOTE_PROVIDER_CONTRACTS["s3"]["retrieval_method"],
        "retrieved_at": retrieved_at,
        "aws_cli": _aws_cli_attestation(),
    }


def _aws_object_response(
    *,
    artifact: Path,
    version_id: str = "3LgX-release-object-version",
) -> str:
    return json.dumps(
        {
            "VersionId": version_id,
            "ETag": '"d41d8cd98f00b204e9800998ecf8427e"',
            "ContentLength": artifact.stat().st_size,
            "ServerSideEncryption": "AES256",
        }
    ) + "\n"


def _aws_debug_stderr(request_id: str) -> str:
    return f"Response headers: {{'x-amz-request-id': '{request_id}'}}\n"


def _backup_receipt_payload(
    *,
    artifact: Path,
    completed_at: str = "2026-07-13T10:00:00Z",
    encrypted: bool = True,
    include_off_host: bool = True,
) -> dict[str, object]:
    sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    payload: dict[str, object] = {
        "schema": dr.RECEIPT_SCHEMA,
        "status": "pass",
        "operation": "backup",
        "completed_at": completed_at,
        "release": _release_receipt(),
        "source": {"host": "source-db", "port": 5432, "database": "propertyquarry"},
        "source_snapshot": _source_snapshot_evidence(plaintext_sha256=sha256),
        "source_schema": _schema_ledger(),
        "source_critical_data": _critical_data_evidence(),
        "artifact": {
            "sha256": sha256,
            "plaintext_sha256": sha256,
            "size_bytes": artifact.stat().st_size,
            "encrypted": encrypted,
            "encryption": "gpg-recipient" if encrypted else "none",
        },
    }
    if include_off_host:
        payload["off_host_object"] = _off_host_object(artifact=artifact)
    return payload


def _restore_receipt_payload(
    *,
    artifact: Path,
    backup_completed_at: str = "2026-07-13T10:00:00Z",
    restore_completed_at: str = "2026-07-13T10:00:20Z",
) -> dict[str, object]:
    schema = _schema_ledger()
    plaintext_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return {
        "schema": dr.RECEIPT_SCHEMA,
        "status": "pass",
        "operation": "restore_drill",
        "completed_at": restore_completed_at,
        "release": _release_receipt(),
        "source": {"host": "source-db", "port": 5432, "database": "propertyquarry"},
        "target": {
            "host": "localhost",
            "port": 5432,
            "database": "propertyquarry_restore_drill_release",
        },
        "source_schema": schema,
        "source_snapshot": _source_snapshot_evidence(plaintext_sha256=plaintext_sha256),
        "restored_schema": schema,
        "source_critical_data": _critical_data_evidence(),
        "restored_critical_data": _critical_data_evidence(),
        "artifact": {
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "size_bytes": artifact.stat().st_size,
            "encrypted": True,
            "backup_completed_at": backup_completed_at,
            "source": "provider_verified_off_host_retrieval",
        },
        "off_host_object": _off_host_object(artifact=artifact),
        "off_host_retrieval": _off_host_retrieval(artifact=artifact),
        "objectives": {
            "rpo_seconds": 10.0,
            "max_rpo_seconds": 60.0,
            "rpo_met": True,
            "rto_seconds": 12.0,
            "max_rto_seconds": 30.0,
            "rto_met": True,
            "rto_scope": list(dr.RESTORE_RTO_SCOPE),
        },
        "verification": {
            "artifact_checksum_valid": True,
            "target_disposable_guard": True,
            "target_identity_valid": True,
            "plaintext_checksum_valid": True,
            "custom_format_list_valid": True,
            "schema_table_count": 23,
            "integrity_query_passed": True,
            "source_schema_contract_valid": True,
            "source_snapshot_contract_valid": True,
            "restored_schema_contract_valid": True,
            "schema_ledger_matches_source": True,
            "critical_data_contract_valid": True,
            "critical_data_exact_match": True,
            "migration_hook_passed": True,
            "schema_migration_forward_verified": True,
            "verification_hook_passed": True,
            "readiness_hook_passed": True,
            "release_identity_matches_backup": True,
            "off_host_object_verified": True,
            "off_host_retrieval_verified": True,
            "aws_cli_attested": True,
            "required_tables": ["delivery_outbox", "property_search_runs"],
            "required_non_empty_tables": ["property_search_runs"],
            "required_table_evidence": {
                "delivery_outbox": {"present": True, "requires_data": False},
                "property_search_runs": {"present": True, "requires_data": True, "row_count": 3},
            },
            "integrity_query_contract_explicit": True,
            "integrity_query_result_sha256": hashlib.sha256(b"1").hexdigest(),
        },
    }


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _which(name: str) -> str:
    return f"/mock/bin/{Path(name).name}"


def _result(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_production_backup_requires_encryption_before_running_commands(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        commands.append(list(command))
        return _result()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_backup(
            artifact_path=tmp_path / "backup.dump",
            overwrite=False,
            environ={
                "EA_RUNTIME_MODE": "prod",
                "DATABASE_URL": "postgresql://owner:secret@db.example/propertyquarry",
                **_release_env(),
            },
            runner=runner,
            which=_which,
        )

    assert exc.value.code == "encryption_required"
    assert commands == []


def test_production_backup_cannot_overwrite_artifact(tmp_path: Path) -> None:
    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_backup(
            artifact_path=tmp_path / "backup.dump.gpg",
            overwrite=True,
            environ={
                "EA_RUNTIME_MODE": "prod",
                "DATABASE_URL": "postgresql://owner:secret@db.example/propertyquarry",
                "PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT": "backup-operator@example.test",
                **_release_env(),
            },
            which=_which,
        )

    assert exc.value.code == "overwrite_forbidden"


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        (
            {
                "provider": "file",
                "backend": "local_copy",
                "bucket": "/tmp",
                "object_key": "propertyquarry.dump.gpg",
            },
            "off_host_identity_invalid",
        ),
        ({"bucket": "localhost"}, "off_host_identity_invalid"),
        ({"bucket": "127.0.0.1"}, "off_host_identity_invalid"),
        ({"region": "http://localhost:9000"}, "off_host_identity_invalid"),
        ({"object_key": "file:/tmp/propertyquarry.dump.gpg"}, "off_host_identity_invalid"),
        ({"object_key": "prod/../propertyquarry.dump.gpg"}, "off_host_identity_invalid"),
    ],
)
def test_off_host_contract_rejects_local_file_and_traversal_identities(
    tmp_path: Path,
    updates: dict[str, str],
    expected_code: str,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    payload = {**_off_host_object(artifact=artifact), **updates}

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._validated_off_host_object(
            payload,
            artifact=_backup_receipt_payload(artifact=artifact)["artifact"],
            now_epoch=datetime(2026, 7, 13, 10, 0, 5, tzinfo=timezone.utc).timestamp(),
        )

    assert exc.value.code == expected_code


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/true --strict",
        "/usr/bin/true harmless-looking-value",
        "/usr/bin/true --api-token=plain-text-secret",
    ],
)
def test_hook_commands_reject_all_argv_without_heuristic_classification(command: str) -> None:
    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._hook_command({"RESTORE_HOOK": command}, "RESTORE_HOOK")

    assert exc.value.code == "hook_arguments_forbidden"


def test_hook_environment_requires_explicit_secret_key_references() -> None:
    env = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "postgresql://owner:database-secret@db.example/propertyquarry",
        "EA_API_TOKEN": "unrelated-secret",
        "RESTORE_HOOK_TOKEN": "explicit-secret",
        "PROPERTYQUARRY_RESTORE_VERIFY_ENV_KEYS": "RESTORE_HOOK_TOKEN",
    }

    process_env = dr._minimal_hook_environment(
        env,
        declared_keys_env="PROPERTYQUARRY_RESTORE_VERIFY_ENV_KEYS",
    )

    assert process_env == {
        "PATH": dr.AWS_CLI_MINIMAL_PATH,
        "LANG": "C",
        "LC_ALL": "C",
        "RESTORE_HOOK_TOKEN": "explicit-secret",
    }

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._minimal_hook_environment(
            {**env, "PROPERTYQUARRY_RESTORE_VERIFY_ENV_KEYS": "LD_PRELOAD", "LD_PRELOAD": "/tmp/x.so"},
            declared_keys_env="PROPERTYQUARRY_RESTORE_VERIFY_ENV_KEYS",
        )
    assert exc.value.code == "hook_env_key_forbidden"


def test_backup_encrypts_validated_dump_and_emits_redacted_checksum_receipt(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    verify_off_host = _trusted_executable(tmp_path, "verify-off-host")
    commands: list[list[str]] = []
    snapshot = _SnapshotConnector()

    def runner(command, **kwargs):
        command = list(command)
        commands.append(command)
        executable = Path(command[0]).name
        snapshot.events.append(("command", executable))
        if executable in {"pg_dump", "psql"}:
            assert snapshot.connection.active is True
        else:
            assert snapshot.connection.active is False
        if executable == "pg_dump":
            assert "--snapshot=00000003-0000001B-1" in command
            Path(command[command.index("--file") + 1]).write_bytes(b"propertyquarry-custom-dump")
        elif executable == "pg_restore":
            assert "--list" in command
            return _result(stdout="; Archive created at 2026-07-13\n")
        elif executable == "gpg":
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"encrypted:" + Path(command[-1]).read_bytes())
        elif executable == "psql":
            sql = command[command.index("--command") + 1]
            assert sql.startswith(
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
                "SET TRANSACTION SNAPSHOT '00000003-0000001B-1'; "
            )
            if "propertyquarry_critical_data:" in sql:
                return _result(stdout=_critical_query_result(sql))
            if "to_regclass" in sql:
                return _result(stdout="t\n")
            return _result(stdout=json.dumps(_schema_ledger()["migrations"]) + "\n")
        elif executable == "verify-off-host":
            hook_env = dict(kwargs["env"])
            assert "DATABASE_URL" not in hook_env
            assert "EA_API_TOKEN" not in hook_env
            return _result(
                stdout=json.dumps(
                    {
                        "provider": "s3",
                        "backend": "aws_s3api",
                        "region": "eu-central-1",
                        "bucket": "propertyquarry-dr-eu",
                        "object_key": "prod/propertyquarry.dump.gpg",
                        "version_id": "release-object-version-1",
                        "etag": "d41d8cd98f00b204e9800998ecf8427e",
                        "sha256": hook_env["PROPERTYQUARRY_BACKUP_ARTIFACT_SHA256"],
                        "size_bytes": int(hook_env["PROPERTYQUARRY_BACKUP_ARTIFACT_SIZE_BYTES"]),
                        "encrypted": True,
                        "off_host": True,
                        "object_exists": True,
                        "checksum_verified": True,
                        "provider_request_id": "verify-request-release-1",
                        "verified_at": dr._utc_iso(1_000.0),
                        "verification_method": dr.REMOTE_PROVIDER_CONTRACTS["s3"][
                            "verification_method"
                        ],
                    }
                )
                + "\n"
            )
        return _result()

    receipt = dr.execute_backup(
        artifact_path=artifact,
        overwrite=False,
        environ={
            "EA_RUNTIME_MODE": "prod",
            "DATABASE_URL": "postgresql://owner:super-secret@db.example/propertyquarry",
            "PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT": "backup-operator@example.test",
            "PROPERTYQUARRY_BACKUP_OFF_HOST_VERIFY_COMMAND": verify_off_host,
            "EA_API_TOKEN": "must-not-reach-provider-hook",
            **_release_env(),
        },
        runner=runner,
        clock=_Clock(1_000.0),
        which=_which,
        snapshot_connector=snapshot,
    )

    assert receipt["status"] == "pass"
    assert receipt["release"] == _release_receipt()
    assert receipt["source_schema"] == _schema_ledger()
    assert receipt["source_snapshot"] == _source_snapshot_evidence(
        plaintext_sha256=hashlib.sha256(b"propertyquarry-custom-dump").hexdigest()
    )
    assert receipt["source_critical_data"] == _critical_data_evidence()
    assert receipt["verification"] == {
        "custom_format_list_valid": True,
        "source_schema_contract_valid": True,
        "source_snapshot_contract_valid": True,
        "source_critical_data_contract_valid": True,
        "off_host_object_verified": True,
    }
    artifact_receipt = dict(receipt["artifact"])
    assert artifact_receipt["encrypted"] is True
    assert artifact_receipt["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert artifact.stat().st_mode & 0o777 == 0o600
    serialized = json.dumps(receipt, sort_keys=True)
    assert "super-secret" not in serialized
    assert "<redacted-database-url>" in serialized
    assert receipt["off_host_object"]["version_id"] == "release-object-version-1"
    assert [Path(command[0]).name for command in commands] == [
        "pg_dump",
        "psql",
        "psql",
        *("psql" for _query in range(2 * len(dr.CRITICAL_DATA_TABLES))),
        "pg_restore",
        "gpg",
        "verify-off-host",
    ]
    assert snapshot.connection.closed is True
    assert snapshot.events.index(("snapshot_rollback", "")) < snapshot.events.index(
        ("command", "pg_restore")
    )
    assert receipt["commands"][-1]["command"] == [verify_off_host]


def test_production_backup_without_provider_verified_off_host_identity_is_not_passing(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    snapshot = _SnapshotConnector()

    def runner(command, **_kwargs):
        command = list(command)
        executable = Path(command[0]).name
        if executable in {"pg_dump", "psql"}:
            assert snapshot.connection.active is True
        if executable == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"propertyquarry-custom-dump")
        elif executable == "gpg":
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"encrypted:" + Path(command[-1]).read_bytes())
        elif executable == "psql":
            sql = command[command.index("--command") + 1]
            if "propertyquarry_critical_data:" in sql:
                return _result(stdout=_critical_query_result(sql))
            if "to_regclass" in sql:
                return _result(stdout="t\n")
            return _result(stdout=json.dumps(_schema_ledger()["migrations"]) + "\n")
        return _result()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_backup(
            artifact_path=artifact,
            overwrite=False,
            environ={
                "EA_RUNTIME_MODE": "prod",
                "DATABASE_URL": "postgresql://owner:secret@db.example/propertyquarry",
                "PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT": "backup-operator@example.test",
                **_release_env(),
            },
            runner=runner,
            clock=_Clock(1_000.0),
            which=_which,
            snapshot_connector=snapshot,
        )

    assert exc.value.code == "off_host_verification_required"


def test_restore_rejects_non_disposable_target_before_running_commands(tmp_path: Path) -> None:
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"dump")
    backup_receipt = tmp_path / "backup.json"
    backup_receipt.write_text(
        json.dumps(_backup_receipt_payload(artifact=artifact)),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=artifact,
            backup_receipt_path=backup_receipt,
            environ={
                "EA_RUNTIME_MODE": "dev",
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": "postgresql://tester@localhost/propertyquarry",
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
            },
            runner=lambda command, **_kwargs: commands.append(list(command)),
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 5, tzinfo=timezone.utc).timestamp()),
            which=_which,
        )

    assert exc.value.code == "target_not_disposable"
    assert commands == []


def test_production_restore_rejects_unencrypted_artifact_before_target_access(tmp_path: Path) -> None:
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"dump")
    backup_receipt = tmp_path / "backup.json"
    backup_receipt.write_text(
        json.dumps(_backup_receipt_payload(artifact=artifact, encrypted=False)),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=artifact,
            backup_receipt_path=backup_receipt,
            environ={
                "EA_RUNTIME_MODE": "prod",
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
                **_release_env(),
            },
            runner=lambda command, **_kwargs: commands.append(list(command)),
            which=_which,
        )

    assert exc.value.code == "encryption_required"
    assert commands == []


def test_env_defined_tables_and_select_one_cannot_replace_canonical_backup_evidence(
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-remote-bytes")
    backup = _backup_receipt_payload(artifact=remote_artifact)
    backup.pop("source_critical_data")
    backup_receipt = tmp_path / "backup.json"
    backup_receipt.write_text(json.dumps(backup), encoding="utf-8")
    commands: list[list[str]] = []

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=tmp_path / "retrieved-backup.dump",
            backup_receipt_path=backup_receipt,
            environ={
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
                "PROPERTYQUARRY_RESTORE_REQUIRED_TABLES": "property_search_runs",
                "PROPERTYQUARRY_RESTORE_REQUIRED_NON_EMPTY_TABLES": "property_search_runs",
                "PROPERTYQUARRY_RESTORE_INTEGRITY_SQL": "SELECT 1;",
                "PROPERTYQUARRY_RESTORE_INTEGRITY_EXPECTED_VALUE": "1",
            },
            runner=lambda command, **_kwargs: commands.append(list(command)),
            which=_which,
        )

    assert exc.value.code == "critical_data_evidence_missing"
    assert commands == []


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        (
            {"PROPERTYQUARRY_RESTORE_OFF_HOST_RETRIEVE_COMMAND": "local-copy --self-assert"},
            "off_host_retrieval_hook_forbidden",
        ),
        (
            {"PROPERTYQUARRY_AWS_BIN": "/tmp/operator-controlled-aws"},
            "off_host_provider_binary_override_forbidden",
        ),
    ],
)
def test_restore_forbids_arbitrary_retrieval_hooks_and_provider_binary_overrides(
    tmp_path: Path,
    override: dict[str, str],
    expected_code: str,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-remote-bytes")
    backup_receipt = tmp_path / "backup.json"
    backup_receipt.write_text(
        json.dumps(_backup_receipt_payload(artifact=remote_artifact)),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=tmp_path / "retrieved-backup.dump",
            backup_receipt_path=backup_receipt,
            environ={
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
                **override,
            },
            runner=lambda command, **_kwargs: commands.append(list(command)),
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 5, tzinfo=timezone.utc).timestamp()),
            which=_which,
        )

    assert exc.value.code == expected_code
    assert commands == []


def test_disposable_restore_drill_verifies_rpo_rto_schema_tables_and_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-custom-dump")
    artifact = tmp_path / "retrieved-backup.dump"
    aws_pin = _install_fake_aws_cli(monkeypatch, tmp_path)
    migration_hook = _trusted_executable(tmp_path, "migrate-restored-schema")
    verification_hook = _trusted_executable(tmp_path, "verify-restore")
    readiness_hook = _trusted_executable(tmp_path, "probe-readiness")
    started = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    backup_receipt = tmp_path / "backup.json"
    backup_payload = _backup_receipt_payload(
        artifact=remote_artifact,
        completed_at=dr._utc_iso(started - 10),
    )
    backup_payload["off_host_object"] = _off_host_object(
        artifact=remote_artifact,
        verified_at=dr._utc_iso(started - 5),
    )
    backup_receipt.write_text(
        json.dumps(backup_payload),
        encoding="utf-8",
    )
    clock = _Clock(started)
    observed: list[tuple[list[str], dict[str, str]]] = []
    retrieval_descriptor: int | None = None

    def runner(command, **kwargs):
        nonlocal retrieval_descriptor
        command = list(command)
        observed.append((command, dict(kwargs.get("env") or {})))
        executable = Path(command[0]).name
        if executable == "aws":
            assert "DATABASE_URL" not in kwargs["env"]
            assert "PROPERTYQUARRY_RESTORE_DATABASE_URL" not in kwargs["env"]
            assert "EA_API_TOKEN" not in kwargs["env"]
            assert "AWS_ENDPOINT_URL" not in kwargs["env"]
            assert kwargs["env"]["PATH"] == dr.AWS_CLI_MINIMAL_PATH
            assert kwargs["env"]["LANG"] == "C"
            if "--version" in command:
                assert kwargs["env"] == {
                    "PATH": dr.AWS_CLI_MINIMAL_PATH,
                    "LANG": "C",
                    "LC_ALL": "C",
                }
                return _result(stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n")
            assert kwargs["env"]["AWS_ACCESS_KEY_ID"] == "provider-access-key"
            assert command[command.index("--region") + 1] == "eu-central-1"
            assert command[command.index("--endpoint-url") + 1] == (
                "https://s3.eu-central-1.amazonaws.com"
            )
            if "head-object" in command:
                clock.advance(1)
                return _result(
                    stdout=_aws_object_response(artifact=remote_artifact),
                    stderr=_aws_debug_stderr("head-request-release-1"),
                )
            assert "get-object" in command
            clock.advance(3)
            Path(command[-1]).write_bytes(remote_artifact.read_bytes())
            return _result(
                stdout=_aws_object_response(artifact=remote_artifact),
                stderr=_aws_debug_stderr("get-request-release-1"),
            )
        if executable == "gpg":
            clock.advance(3)
            assert command[-1].startswith("/proc/self/fd/")
            retrieval_descriptor = int(command[-1].rsplit("/", 1)[-1])
            assert kwargs["pass_fds"] == (retrieval_descriptor,)
            os.fstat(retrieval_descriptor)
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(Path(command[-1]).read_bytes())
            return _result()
        if executable == "pg_restore":
            if "--list" in command:
                assert retrieval_descriptor is not None
                os.fstat(retrieval_descriptor)
                clock.advance(2)
                return _result(stdout="; valid archive\n")
            assert "--clean" in command
            assert "--single-transaction" in command
            assert "--create" not in command
            clock.advance(12)
            return _result()
        if executable == "psql":
            sql = command[command.index("--command") + 1]
            if "propertyquarry_critical_data:" in sql:
                return _result(stdout=_critical_query_result(sql))
            if "current_database" in sql:
                return _result(stdout="propertyquarry_restore_drill_ci\n")
            if "COUNT(*)" in sql:
                return _result(stdout="23\n")
            if "json_agg" in sql:
                return _result(stdout=json.dumps(_schema_ledger()["migrations"]) + "\n")
            if "to_regclass" in sql:
                return _result(stdout="t\n")
            return _result(stdout="1\n")
        if executable in {"migrate-restored-schema", "verify-restore", "probe-readiness"}:
            assert kwargs["env"]["DATABASE_URL"].endswith("/propertyquarry_restore_drill_ci")
            assert kwargs["env"]["PROPERTYQUARRY_RESTORE_DRILL"] == "1"
            assert "EA_API_TOKEN" not in kwargs["env"]
            if executable == "verify-restore":
                assert kwargs["env"]["RESTORE_HOOK_TOKEN"] == "explicit-hook-secret"
            else:
                assert "RESTORE_HOOK_TOKEN" not in kwargs["env"]
        return _result()

    receipt = dr.execute_restore_drill(
        artifact_path=artifact,
        backup_receipt_path=backup_receipt,
        environ={
            "EA_RUNTIME_MODE": "dev",
            "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                "postgresql://tester:target-secret@localhost/propertyquarry_restore_drill_ci"
            ),
            "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
            "PROPERTYQUARRY_BACKUP_MAX_AGE_SECONDS": "60",
            "PROPERTYQUARRY_RESTORE_MAX_DURATION_SECONDS": "30",
            "PROPERTYQUARRY_RESTORE_REQUIRED_TABLES": "execution_sessions,artifacts",
            "PROPERTYQUARRY_RESTORE_REQUIRED_NON_EMPTY_TABLES": "artifacts",
            "PROPERTYQUARRY_RESTORE_INTEGRITY_SQL": "SELECT 1;",
            "PROPERTYQUARRY_RESTORE_INTEGRITY_EXPECTED_VALUE": "1",
            "PROPERTYQUARRY_RESTORE_MIGRATION_COMMAND": migration_hook,
            "PROPERTYQUARRY_RESTORE_VERIFY_COMMAND": verification_hook,
            "PROPERTYQUARRY_RESTORE_READINESS_COMMAND": readiness_hook,
            "PROPERTYQUARRY_RESTORE_VERIFY_ENV_KEYS": "RESTORE_HOOK_TOKEN",
            "RESTORE_HOOK_TOKEN": "explicit-hook-secret",
            "EA_API_TOKEN": "must-not-reach-provider-or-restore-hooks",
            "AWS_ACCESS_KEY_ID": "provider-access-key",
            "AWS_SECRET_ACCESS_KEY": "provider-secret-key",
            "AWS_ENDPOINT_URL": "http://localhost:9000",
            **_release_env(),
        },
        runner=runner,
        clock=clock,
        which=_which,
    )

    assert receipt["status"] == "pass"
    assert retrieval_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(retrieval_descriptor)
    assert receipt["objectives"] == {
        "rpo_seconds": 10.0,
        "max_rpo_seconds": 60.0,
        "rpo_met": True,
        "rto_seconds": 21.0,
        "max_rto_seconds": 30.0,
        "rto_met": True,
        "rto_scope": list(dr.RESTORE_RTO_SCOPE),
    }
    verification = dict(receipt["verification"])
    assert verification["schema_table_count"] == 23
    assert verification["schema_ledger_matches_source"] is True
    assert verification["migration_hook_passed"] is True
    assert verification["schema_migration_forward_verified"] is True
    assert verification["required_tables"] == ["execution_sessions", "artifacts"]
    assert verification["required_non_empty_tables"] == ["artifacts"]
    assert verification["required_table_evidence"]["execution_sessions"] == {
        "present": True,
        "requires_data": False,
    }
    assert verification["required_table_evidence"]["artifacts"] == {
        "present": True,
        "requires_data": True,
        "row_count": 23,
    }
    assert verification["off_host_retrieval_verified"] is True
    assert verification["verification_hook_passed"] is True
    assert verification["readiness_hook_passed"] is True
    serialized = json.dumps(receipt, sort_keys=True)
    assert "target-secret" not in serialized
    assert "<redacted-database-url>" in serialized
    assert receipt["release"] == _release_receipt()
    assert receipt["source_schema"] == receipt["restored_schema"] == _schema_ledger()
    assert receipt["source_critical_data"] == receipt["restored_critical_data"]
    assert receipt["source_critical_data"] == _critical_data_evidence()
    assert verification["critical_data_contract_valid"] is True
    assert verification["critical_data_exact_match"] is True
    assert receipt["off_host_object"]["version_id"] == "3LgX-release-object-version"
    assert receipt["off_host_retrieval"]["version_id"] == "3LgX-release-object-version"
    assert receipt["off_host_retrieval"]["provider_request_id"] == (
        "head-request-release-1:get-request-release-1"
    )
    assert receipt["artifact"]["source"] == "provider_verified_off_host_retrieval"
    assert [entry["command"] for entry in receipt["commands"][:3]] == [
        [aws_pin["path"], "--version"],
        [
            aws_pin["path"],
            "s3api",
            "head-object",
            "<immutable-identity-redacted>",
        ],
        [
            aws_pin["path"],
            "s3api",
            "get-object",
            "<immutable-identity-redacted>",
        ],
    ]
    assert [entry["command"] for entry in receipt["commands"] if entry["step"].endswith("hook")] == [
        [migration_hook],
        [verification_hook],
        [readiness_hook],
    ]
    assert [Path(command[0]).name for command, _env in observed].count("pg_restore") == 2


def test_retrieved_byte_mismatch_stops_before_target_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-remote-bytes")
    artifact = tmp_path / "retrieved-backup.dump"
    _install_fake_aws_cli(monkeypatch, tmp_path)
    backup_receipt = tmp_path / "backup.json"
    backup_payload = _backup_receipt_payload(artifact=remote_artifact)
    backup_receipt.write_text(
        json.dumps(backup_payload),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        command = list(command)
        commands.append(command)
        assert Path(command[0]).name == "aws"
        if "--version" in command:
            return _result(stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n")
        if "head-object" in command:
            return _result(
                stdout=_aws_object_response(artifact=remote_artifact),
                stderr=_aws_debug_stderr("head-request"),
            )
        Path(command[-1]).write_bytes(b"tampered-retrieval")
        return _result(
            stdout=_aws_object_response(artifact=remote_artifact),
            stderr=_aws_debug_stderr("get-request"),
        )

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=artifact,
            backup_receipt_path=backup_receipt,
            environ={
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
            },
            runner=runner,
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 15, tzinfo=timezone.utc).timestamp()),
            which=_which,
        )

    assert exc.value.code == "off_host_retrieval_artifact_mismatch"
    assert [Path(command[0]).name for command in commands] == ["aws", "aws", "aws"]


def test_restore_requires_pinned_provider_attestation_and_never_falls_back_to_local_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-remote-bytes")
    retrieval_destination = tmp_path / "retrieved-backup.dump"
    backup_receipt = tmp_path / "backup.json"
    backup_receipt.write_text(
        json.dumps(_backup_receipt_payload(artifact=remote_artifact)),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    missing_aws = tmp_path / "missing-pinned-aws"
    configured_pin = _aws_cli_release_pin(
        path=str(missing_aws),
        sha256=hashlib.sha256(b"missing-pinned-aws").hexdigest(),
    )
    monkeypatch.setattr(
        dr,
        "_load_aws_cli_release_pin",
        lambda: dict(configured_pin),
    )

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=retrieval_destination,
            backup_receipt_path=backup_receipt,
            environ={
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
            },
            runner=lambda command, **_kwargs: commands.append(list(command)),
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 15, tzinfo=timezone.utc).timestamp()),
            which=lambda name: None if Path(name).name == "aws" else _which(name),
        )

    assert exc.value.code == "aws_cli_path_invalid"
    assert commands == []

    retrieval_destination.write_bytes(remote_artifact.read_bytes())
    with pytest.raises(dr.DisasterRecoveryError) as existing_exc:
        dr.execute_restore_drill(
            artifact_path=retrieval_destination,
            backup_receipt_path=backup_receipt,
            environ={
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
            },
            runner=lambda command, **_kwargs: commands.append(list(command)),
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 15, tzinfo=timezone.utc).timestamp()),
            which=_which,
        )

    assert existing_exc.value.code == "off_host_retrieval_destination_exists"
    assert commands == []


def test_restore_rejects_provider_retrieval_for_a_different_immutable_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-remote-bytes")
    retrieval_destination = tmp_path / "retrieved-backup.dump"
    _install_fake_aws_cli(monkeypatch, tmp_path)
    backup_receipt = tmp_path / "backup.json"
    backup_receipt.write_text(
        json.dumps(_backup_receipt_payload(artifact=remote_artifact)),
        encoding="utf-8",
    )

    def runner(command, **_kwargs):
        command = list(command)
        assert Path(command[0]).name == "aws"
        if "--version" in command:
            return _result(stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n")
        return _result(
            stdout=_aws_object_response(
                artifact=remote_artifact,
                version_id="different-provider-version",
            ),
            stderr=_aws_debug_stderr("head-request"),
        )

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=retrieval_destination,
            backup_receipt_path=backup_receipt,
            environ={
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
            },
            runner=runner,
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 15, tzinfo=timezone.utc).timestamp()),
            which=_which,
        )

    assert exc.value.code == "off_host_retrieval_identity_mismatch"


def test_aws_cli_attestation_ignores_path_wrapper_and_executes_open_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    approved_pin = _install_fake_aws_cli(monkeypatch, tmp_path)
    wrapper_dir = tmp_path / "wrapper-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "aws"
    wrapper.write_text("#!/bin/sh\necho compromised\n", encoding="utf-8")
    wrapper.chmod(0o755)
    observed: list[tuple[list[str], dict[str, object]]] = []

    def runner(command, **kwargs):
        observed.append((list(command), dict(kwargs)))
        return _result(stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n")

    attestation = dr._attest_aws_cli(
        env={"PATH": str(wrapper_dir), "UNRELATED_SECRET": "must-not-leak"},
        runner=runner,
        commands=[],
    )

    assert attestation["path"] == approved_pin["path"]
    assert observed[0][0] == [approved_pin["path"], "--version"]
    assert str(observed[0][1]["executable"]).startswith("/proc/self/fd/")
    assert observed[0][1]["env"] == {
        "PATH": dr.AWS_CLI_MINIMAL_PATH,
        "LANG": "C",
        "LC_ALL": "C",
    }
    assert str(wrapper) not in observed[0][0]


def test_aws_cli_attestation_rejects_symlink_wrong_hash_version_and_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    approved_pin = _install_fake_aws_cli(monkeypatch, tmp_path)
    approved_path = Path(approved_pin["path"])

    symlink = tmp_path / "aws-link"
    symlink.symlink_to(approved_path)
    monkeypatch.setattr(
        dr,
        "_load_aws_cli_release_pin",
        lambda: {**approved_pin, "path": str(symlink)},
    )
    with pytest.raises(dr.DisasterRecoveryError) as symlink_exc:
        dr._attest_aws_cli(
            env={},
            runner=lambda *_args, **_kwargs: _result(),
            commands=[],
        )
    assert symlink_exc.value.code == "aws_cli_path_untrusted"

    monkeypatch.setattr(dr, "_load_aws_cli_release_pin", lambda: dict(approved_pin))
    monkeypatch.setattr(
        dr,
        "_load_aws_cli_release_pin",
        lambda: {**approved_pin, "sha256": "0" * 64},
    )
    with pytest.raises(dr.DisasterRecoveryError) as hash_exc:
        dr._attest_aws_cli(
            env={},
            runner=lambda *_args, **_kwargs: _result(),
            commands=[],
        )
    assert hash_exc.value.code == "aws_cli_sha256_mismatch"

    monkeypatch.setattr(dr, "_load_aws_cli_release_pin", lambda: dict(approved_pin))

    with pytest.raises(dr.DisasterRecoveryError) as version_exc:
        dr._attest_aws_cli(
            env={},
            runner=lambda *_args, **_kwargs: _result(
                stdout="aws-cli/2.27.48 Python/3 test/Linux\n"
            ),
            commands=[],
        )
    assert version_exc.value.code == "aws_cli_version_mismatch"

    approved_path.chmod(0o775)
    with pytest.raises(dr.DisasterRecoveryError) as mode_exc:
        dr._attest_aws_cli(
            env={},
            runner=lambda *_args, **_kwargs: _result(),
            commands=[],
        )
    assert mode_exc.value.code == "aws_cli_path_untrusted"


def test_aws_cli_attestation_detects_path_replacement_during_version_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    approved_pin = _install_fake_aws_cli(monkeypatch, tmp_path)
    approved_path = Path(approved_pin["path"])
    approved_bytes = approved_path.read_bytes()

    def runner(_command, **_kwargs):
        approved_path.unlink()
        approved_path.write_bytes(approved_bytes)
        approved_path.chmod(0o755)
        return _result(stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n")

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._attest_aws_cli(env={}, runner=runner, commands=[])

    assert exc.value.code == "aws_cli_binary_race"


def test_checked_in_aws_cli_release_pin_is_explicitly_configured() -> None:
    raw = dr.AWS_CLI_RELEASE_PIN_PATH.read_bytes()
    expected = dr._parse_aws_cli_release_pin(raw)
    actual = _REAL_LOAD_AWS_CLI_RELEASE_PIN()

    assert actual == expected
    assert actual["status"] == dr.AWS_CLI_RELEASE_PIN_CONFIGURED


def test_unconfigured_aws_cli_release_pin_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_pin_path = tmp_path / "aws_cli_release_pin.json"
    release_pin_path.write_text(
        json.dumps(
            {
                "path": dr.AWS_CLI_RELEASE_PIN_UNCONFIGURED,
                "schema": dr.AWS_CLI_RELEASE_PIN_SCHEMA,
                "sha256": dr.AWS_CLI_RELEASE_PIN_UNCONFIGURED,
                "status": dr.AWS_CLI_RELEASE_PIN_UNCONFIGURED,
                "version": dr.AWS_CLI_RELEASE_PIN_UNCONFIGURED,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    release_pin_path.chmod(0o600)
    monkeypatch.setattr(dr, "AWS_CLI_RELEASE_PIN_PATH", release_pin_path)

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        _REAL_LOAD_AWS_CLI_RELEASE_PIN()

    assert exc.value.code == "aws_cli_release_pin_unconfigured"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PROPERTYQUARRY_AWS_CLI_PATH", "/tmp/operator-selected-malicious-aws"),
        ("PROPERTYQUARRY_AWS_CLI_APPROVED_VERSION", "99.99.99"),
        ("PROPERTYQUARRY_AWS_CLI_APPROVED_SHA256", "0" * 64),
    ],
)
def test_aws_cli_release_pin_rejects_all_environment_overrides_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    _install_fake_aws_cli(monkeypatch, tmp_path)
    commands: list[list[str]] = []

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._attest_aws_cli(
            env={name: value},
            runner=lambda command, **_kwargs: commands.append(list(command)),
            commands=[],
        )

    assert exc.value.code == "aws_cli_release_pin_override_forbidden"
    assert commands == []


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--aws-cli-path", "/tmp/operator-selected-malicious-aws"),
        ("--aws-cli-approved-version", "99.99.99"),
        ("--aws-cli-approved-sha256", "0" * 64),
    ],
)
def test_release_gate_cli_has_no_aws_pin_override_surface(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    with pytest.raises(SystemExit) as exc:
        dr._parser().parse_args(
            [
                "release-gate",
                "--backup-receipt",
                str(tmp_path / "backup.json"),
                "--restore-receipt",
                str(tmp_path / "restore.json"),
                "--release-commit-sha",
                RELEASE_COMMIT_SHA,
                "--image-digest",
                RELEASE_IMAGE_DIGEST,
                flag,
                value,
                "--receipt",
                str(tmp_path / "release.json"),
            ]
        )

    assert exc.value.code == 2


@pytest.mark.parametrize(
    "keyword",
    [
        "aws_cli_path",
        "aws_cli_approved_version",
        "aws_cli_approved_sha256",
    ],
)
def test_release_gate_api_has_no_caller_selected_aws_pin_surface(
    tmp_path: Path,
    keyword: str,
) -> None:
    arguments: dict[str, object] = {
        "backup_receipt_path": tmp_path / "backup.json",
        "restore_receipt_path": tmp_path / "restore.json",
        "release_commit_sha": RELEASE_COMMIT_SHA,
        "image_digest": RELEASE_IMAGE_DIGEST,
        keyword: "/tmp/operator-selected-value",
    }

    with pytest.raises(TypeError):
        dr.verify_release_dr_evidence(**arguments)


def test_release_gate_rejects_legacy_aws_pin_environment_before_receipt_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "PROPERTYQUARRY_AWS_CLI_PATH",
        "/tmp/operator-selected-malicious-aws",
    )

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=tmp_path / "missing-backup.json",
            restore_receipt_path=tmp_path / "missing-restore.json",
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
        )

    assert exc.value.code == "aws_cli_release_pin_override_forbidden"


def test_aws_cli_attestation_rejects_path_substitution_against_canonical_pin() -> None:
    attestation = _aws_cli_attestation()
    attestation["path"] = "/tmp/operator-selected-malicious-aws"

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._validated_aws_cli_attestation(attestation, label="Test")

    assert exc.value.code == "aws_cli_release_pin_mismatch"


def test_retrieval_destination_rejects_symlink_and_detects_path_replacement(tmp_path: Path) -> None:
    destination = tmp_path / "retrieved.dump"
    target = tmp_path / "attacker.dump"
    target.write_bytes(b"attacker")
    destination.symlink_to(target)

    with pytest.raises(dr.DisasterRecoveryError) as symlink_exc:
        with dr._exclusive_retrieval_destination(destination):
            pass
    assert symlink_exc.value.code == "off_host_retrieval_destination_exists"

    destination.unlink()
    with pytest.raises(dr.DisasterRecoveryError) as race_exc:
        with dr._exclusive_retrieval_destination(destination):
            destination.unlink()
            destination.symlink_to(target)
    assert race_exc.value.code == "off_host_retrieval_destination_race"


@pytest.mark.parametrize(
    "etag",
    [
        '"d41d8cd98f00b204e9800998ecf8427e',
        'd41d8cd98f00b204e9800998ecf8427e"',
    ],
)
def test_s3_etag_normalization_rejects_asymmetric_quotes(etag: str) -> None:
    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._normalize_s3_etag(etag)

    assert exc.value.code == "off_host_etag_invalid"
    assert dr._normalize_s3_etag('"d41d8cd98f00b204e9800998ecf8427e"') == (
        "d41d8cd98f00b204e9800998ecf8427e"
    )


def test_provider_retrieval_uses_minimal_environment_and_binds_cli_attestation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-remote-bytes")
    destination = tmp_path / "retrieved-backup.dump"
    aws_pin = _install_fake_aws_cli(monkeypatch, tmp_path)
    observed_envs: list[dict[str, str]] = []

    def runner(command, **kwargs):
        command = list(command)
        observed_envs.append(dict(kwargs["env"]))
        if "--version" in command:
            return _result(stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n")
        if "head-object" in command:
            return _result(
                stdout=_aws_object_response(artifact=remote_artifact),
                stderr=_aws_debug_stderr("head-request-minimal-env"),
            )
        Path(command[-1]).write_bytes(remote_artifact.read_bytes())
        return _result(
            stdout=_aws_object_response(artifact=remote_artifact),
            stderr=_aws_debug_stderr("get-request-minimal-env"),
        )

    artifact_receipt = dict(_backup_receipt_payload(artifact=remote_artifact)["artifact"])
    with dr._retrieve_off_host_object(
        env={
            "AWS_ACCESS_KEY_ID": "provider-access",
            "AWS_SECRET_ACCESS_KEY": "provider-secret",
            "DATABASE_URL": "postgresql://owner:secret@db.example/propertyquarry",
            "HOME": "/tmp/operator-home",
            "PATH": "/tmp/operator-wrapper-bin",
            "UNRELATED_SECRET": "must-not-leak",
        },
        runner=runner,
        commands=[],
        off_host_object=_off_host_object(artifact=remote_artifact),
        artifact=artifact_receipt,
        destination=destination,
        clock=_Clock(datetime(2026, 7, 13, 10, 0, 15, tzinfo=timezone.utc).timestamp()),
    ) as (retrieval, descriptor):
        assert retrieval["aws_cli"]["path"] == aws_pin["path"]
        assert os.pread(descriptor, len(remote_artifact.read_bytes()), 0) == remote_artifact.read_bytes()
    assert observed_envs[0] == {
        "PATH": dr.AWS_CLI_MINIMAL_PATH,
        "LANG": "C",
        "LC_ALL": "C",
    }
    for provider_env in observed_envs[1:]:
        assert provider_env["PATH"] == dr.AWS_CLI_MINIMAL_PATH
        assert provider_env["AWS_ACCESS_KEY_ID"] == "provider-access"
        assert provider_env["AWS_SECRET_ACCESS_KEY"] == "provider-secret"
        assert "DATABASE_URL" not in provider_env
        assert "HOME" not in provider_env
        assert "UNRELATED_SECRET" not in provider_env


def test_provider_retrieval_detects_destination_swap_during_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump"
    remote_artifact.write_bytes(b"verified-remote-bytes")
    destination = tmp_path / "retrieved-backup.dump"
    attacker_target = tmp_path / "attacker.dump"
    attacker_target.write_bytes(b"attacker")
    _install_fake_aws_cli(monkeypatch, tmp_path)

    def runner(command, **_kwargs):
        command = list(command)
        if "--version" in command:
            return _result(stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n")
        if "head-object" in command:
            return _result(
                stdout=_aws_object_response(artifact=remote_artifact),
                stderr=_aws_debug_stderr("head-request-race"),
            )
        Path(command[-1]).write_bytes(remote_artifact.read_bytes())
        destination.unlink()
        destination.symlink_to(attacker_target)
        return _result(
            stdout=_aws_object_response(artifact=remote_artifact),
            stderr=_aws_debug_stderr("get-request-race"),
        )

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        with dr._retrieve_off_host_object(
            env={},
            runner=runner,
            commands=[],
            off_host_object=_off_host_object(artifact=remote_artifact),
            artifact=dict(_backup_receipt_payload(artifact=remote_artifact)["artifact"]),
            destination=destination,
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 15, tzinfo=timezone.utc).timestamp()),
        ):
            pass

    assert exc.value.code == "off_host_retrieval_destination_race"


def test_post_retrieval_path_swap_cannot_substitute_gpg_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_artifact = tmp_path / "remote-backup.dump.gpg"
    remote_artifact.write_bytes(b"verified-encrypted-remote-bytes")
    destination = tmp_path / "retrieved-backup.dump.gpg"
    attacker_target = tmp_path / "attacker.dump.gpg"
    attacker_target.write_bytes(b"attacker-controlled-bytes")
    _install_fake_aws_cli(monkeypatch, tmp_path)
    backup_receipt = tmp_path / "backup.json"
    backup_receipt.write_text(
        json.dumps(_backup_receipt_payload(artifact=remote_artifact)),
        encoding="utf-8",
    )
    executed: list[str] = []
    gpg_inputs: list[bytes] = []

    def runner(command, **kwargs):
        command = list(command)
        executable = Path(command[0]).name
        executed.append(executable)
        if executable == "aws":
            if "--version" in command:
                return _result(
                    stdout=f"aws-cli/{AWS_CLI_APPROVED_VERSION} Python/3 test/Linux\n"
                )
            if "head-object" in command:
                return _result(
                    stdout=_aws_object_response(artifact=remote_artifact),
                    stderr=_aws_debug_stderr("head-request-post-retrieval-race"),
                )
            Path(command[-1]).write_bytes(remote_artifact.read_bytes())
            return _result(
                stdout=_aws_object_response(artifact=remote_artifact),
                stderr=_aws_debug_stderr("get-request-post-retrieval-race"),
            )
        if executable == "gpg":
            assert command[-1].startswith("/proc/self/fd/")
            descriptor = int(command[-1].rsplit("/", 1)[-1])
            assert kwargs["pass_fds"] == (descriptor,)
            destination.unlink()
            destination.symlink_to(attacker_target)
            gpg_inputs.append(Path(command[-1]).read_bytes())
            Path(command[command.index("--output") + 1]).write_bytes(gpg_inputs[-1])
            return _result()
        pytest.fail(f"Path-swap restore unexpectedly reached {executable}")

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.execute_restore_drill(
            artifact_path=destination,
            backup_receipt_path=backup_receipt,
            environ={
                "PROPERTYQUARRY_RESTORE_DATABASE_URL": (
                    "postgresql://tester@localhost/propertyquarry_restore_drill_ci"
                ),
                "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM": dr.DISPOSABLE_CONFIRMATION,
            },
            runner=runner,
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 15, tzinfo=timezone.utc).timestamp()),
            which=_which,
        )

    assert exc.value.code == "off_host_retrieval_destination_race"
    assert gpg_inputs == [remote_artifact.read_bytes()]
    assert gpg_inputs[0] != attacker_target.read_bytes()
    assert executed == ["aws", "aws", "aws", "gpg"]


def test_release_gate_rejects_cli_attestation_not_matching_approved_pin(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(_backup_receipt_payload(artifact=artifact)), encoding="utf-8")
    restore = _restore_receipt_payload(artifact=artifact)
    restore["off_host_retrieval"]["aws_cli"]["release_pin"]["manifest_sha256"] = "d" * 64
    restore_path.write_text(json.dumps(restore), encoding="utf-8")

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()),
        )

    assert exc.value.code == "aws_cli_release_pin_mismatch"


def test_cli_validation_failure_writes_a_redacted_failure_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "backup.dump"
    receipt_path = tmp_path / "backup.failure.json"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_BACKUP_DATABASE_URL", raising=False)

    exit_code = dr.main(
        [
            "backup",
            "--artifact",
            str(artifact),
            "--receipt",
            str(receipt_path),
        ]
    )

    assert exit_code == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "fail"
    assert receipt["operation"] == "backup"
    assert receipt["error"]["code"] == "database_url_missing"
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert not artifact.exists()


def test_release_gate_binds_recent_encrypted_off_host_restore_to_exact_release_and_schema(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    source_schema = _schema_ledger_prefix(4)
    backup = _backup_receipt_payload(artifact=artifact)
    backup["source_schema"] = source_schema
    restore = _restore_receipt_payload(artifact=artifact)
    restore["source_schema"] = source_schema
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    restore_path.write_text(json.dumps(restore), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    receipt = dr.verify_release_dr_evidence(
        backup_receipt_path=backup_path,
        restore_receipt_path=restore_path,
        release_commit_sha=RELEASE_COMMIT_SHA,
        image_digest=RELEASE_IMAGE_DIGEST,
        max_age_seconds=60,
        clock=_Clock(now),
    )

    assert receipt["status"] == "pass"
    assert receipt["operation"] == "release_gate"
    assert receipt["release"] == _release_receipt()
    assert receipt["source_schema"] == source_schema
    assert receipt["source_snapshot"] == _source_snapshot_evidence(
        plaintext_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest()
    )
    assert receipt["restored_schema"] == _schema_ledger()
    assert receipt["off_host_object"]["version_id"] == "3LgX-release-object-version"
    assert receipt["aws_cli"]["release_pin"] == _aws_cli_release_pin()
    assert receipt["aws_cli"]["release_pin"]["manifest_repo_path"] == (
        dr.AWS_CLI_RELEASE_PIN_REPO_PATH
    )
    assert receipt["evidence"]["critical_data_contract"] == {
        "contract_name": dr.CRITICAL_DATA_CONTRACT_NAME,
        "contract_version": dr.CRITICAL_DATA_CONTRACT_VERSION,
        "evidence_version": dr.CRITICAL_DATA_EVIDENCE_VERSION,
        "contract_fingerprint_sha256": _critical_data_evidence()[
            "contract_fingerprint_sha256"
        ],
        "fingerprint_algorithm": dr.CRITICAL_DATA_FINGERPRINT_ALGORITHM,
        "chunk_size": dr.CRITICAL_DATA_CHUNK_SIZE,
        "max_row_bytes": dr.CRITICAL_DATA_MAX_ROW_BYTES,
        "max_chunks": dr.CRITICAL_DATA_MAX_CHUNKS,
        "max_supported_rows": dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS,
    }
    assert receipt["evidence"]["critical_data_tables"] == [
        {
            "schema": row["schema"],
            "table": row["table"],
            "row_count": row["row_count"],
            "chunk_count": row["chunk_count"],
            "merkle_root_sha256": row["merkle_root_sha256"],
            "fingerprint_sha256": row["fingerprint_sha256"],
        }
        for row in _critical_data_evidence()["tables"]
    ]
    assert all(receipt["verification"].values())


@pytest.mark.parametrize("receipt_kind", ("backup", "restore"))
def test_release_gate_rejects_symlinked_receipt_inputs(
    tmp_path: Path,
    receipt_kind: str,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup_real = tmp_path / "backup.real.json"
    restore_real = tmp_path / "restore.real.json"
    backup_real.write_text(
        json.dumps(_backup_receipt_payload(artifact=artifact)),
        encoding="utf-8",
    )
    restore_real.write_text(
        json.dumps(_restore_receipt_payload(artifact=artifact)),
        encoding="utf-8",
    )
    backup_path = backup_real
    restore_path = restore_real
    if receipt_kind == "backup":
        backup_path = tmp_path / "backup.json"
        backup_path.symlink_to(backup_real.name)
    else:
        restore_path = tmp_path / "restore.json"
        restore_path.symlink_to(restore_real.name)

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
        )

    assert exc.value.code == "dr_receipt_invalid"


@pytest.mark.parametrize("unsafe_kind", ("fifo", "hardlink", "peer-writable"))
def test_release_gate_rejects_unsafe_receipt_file_types_and_modes(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup_real = tmp_path / "backup.real.json"
    restore_path = tmp_path / "restore.json"
    backup_real.write_text(
        json.dumps(_backup_receipt_payload(artifact=artifact)),
        encoding="utf-8",
    )
    restore_path.write_text(
        json.dumps(_restore_receipt_payload(artifact=artifact)),
        encoding="utf-8",
    )
    backup_path = tmp_path / "backup.json"
    if unsafe_kind == "fifo":
        os.mkfifo(backup_path, mode=0o600)
    elif unsafe_kind == "hardlink":
        os.link(backup_real, backup_path)
    else:
        backup_path.write_bytes(backup_real.read_bytes())
        backup_path.chmod(0o660)

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
        )

    assert exc.value.code == "dr_receipt_invalid"


def test_receipt_input_rejects_non_sticky_peer_writable_ancestor(
    tmp_path: Path,
) -> None:
    peer_writable_ancestor = tmp_path / "peer-writable"
    peer_writable_ancestor.mkdir()
    trusted_parent = peer_writable_ancestor / "trusted"
    trusted_parent.mkdir(mode=0o700)
    receipt_path = trusted_parent / "backup.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema": dr.RECEIPT_SCHEMA,
                "status": "pass",
                "operation": "backup",
            }
        ),
        encoding="utf-8",
    )
    peer_writable_ancestor.chmod(0o777)

    try:
        with pytest.raises(dr.DisasterRecoveryError) as exc:
            dr._load_passing_operation_receipt(
                receipt_path,
                operation="backup",
            )
    finally:
        peer_writable_ancestor.chmod(0o700)

    assert exc.value.code == "dr_receipt_invalid"


def test_release_gate_rejects_receipt_replacement_while_reading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_bytes = json.dumps(
        _backup_receipt_payload(artifact=artifact),
    ).encode("utf-8")
    backup_path.write_bytes(backup_bytes)
    restore_path.write_text(
        json.dumps(_restore_receipt_payload(artifact=artifact)),
        encoding="utf-8",
    )
    original_read = dr.os.read
    replaced = False

    def replace_during_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            backup_path.rename(tmp_path / "backup.displaced.json")
            backup_path.write_bytes(backup_bytes)
        return original_read(descriptor, size)

    monkeypatch.setattr(dr.os, "read", replace_during_read)

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
        )

    assert replaced is True
    assert exc.value.code == "dr_receipt_invalid"


def test_release_gate_rejects_ambiguous_receipt_json(tmp_path: Path) -> None:
    backup_path = tmp_path / "backup.json"
    backup_path.write_text(
        '{"schema":"propertyquarry.postgres_dr_receipt.v3",'
        '"schema":"propertyquarry.postgres_dr_receipt.v3",'
        '"status":"pass","operation":"backup"}',
        encoding="utf-8",
    )

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._load_passing_operation_receipt(
            backup_path,
            operation="backup",
        )

    assert exc.value.code == "dr_receipt_invalid"


def test_release_gate_rejects_receipts_for_a_different_image_digest(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(_backup_receipt_payload(artifact=artifact)), encoding="utf-8")
    restore_path.write_text(json.dumps(_restore_receipt_payload(artifact=artifact)), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest="sha256:" + "c" * 64,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert exc.value.code == "release_evidence_mismatch"


def test_release_gate_rejects_non_finite_freshness_boundary(tmp_path: Path) -> None:
    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=tmp_path / "unused-backup.json",
            restore_receipt_path=tmp_path / "unused-restore.json",
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=float("nan"),
        )

    assert exc.value.code == "release_evidence_max_age_invalid"


def test_release_gate_rejects_unverified_off_host_object_identity(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup = _backup_receipt_payload(artifact=artifact)
    backup["off_host_object"] = {**dict(backup["off_host_object"]), "version_id": ""}
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    restore_path.write_text(json.dumps(_restore_receipt_payload(artifact=artifact)), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert exc.value.code == "off_host_version_invalid"


def test_release_gate_rejects_restore_without_provider_retrieval_evidence(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    restore = _restore_receipt_payload(artifact=artifact)
    restore.pop("off_host_retrieval")
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(_backup_receipt_payload(artifact=artifact)), encoding="utf-8")
    restore_path.write_text(json.dumps(restore), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert exc.value.code == "off_host_retrieval_invalid"


def test_release_gate_rejects_empty_canonical_required_data_but_allows_optional_empty_ledgers(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup = _backup_receipt_payload(artifact=artifact)
    restore = _restore_receipt_payload(artifact=artifact)
    empty_evidence = _critical_data_evidence(row_counts={"property_search_runs": 0})
    backup["source_critical_data"] = empty_evidence
    restore["source_critical_data"] = empty_evidence
    restore["restored_critical_data"] = empty_evidence
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    restore_path.write_text(json.dumps(restore), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert exc.value.code == "critical_data_required_table_empty"
    delivery_outbox = next(
        row for row in empty_evidence["tables"] if row["table"] == "delivery_outbox"
    )
    assert delivery_outbox["row_count"] == 0
    assert delivery_outbox["data_required"] is False


@pytest.mark.parametrize(
    "restored_evidence",
    [
        _critical_data_evidence(row_counts={"property_search_runs": 4}),
        _critical_data_evidence(fingerprint_salt="tampered-row-content"),
    ],
    ids=["row-count-mismatch", "row-fingerprint-mismatch"],
)
def test_release_gate_rejects_canonical_critical_data_count_or_fingerprint_mismatch(
    tmp_path: Path,
    restored_evidence: dict[str, object],
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    restore = _restore_receipt_payload(artifact=artifact)
    restore["restored_critical_data"] = restored_evidence
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(
        json.dumps(_backup_receipt_payload(artifact=artifact)),
        encoding="utf-8",
    )
    restore_path.write_text(json.dumps(restore), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert exc.value.code == "release_critical_data_mismatch"


def test_release_gate_rejects_restored_migration_name_or_checksum_drift(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    restore = _restore_receipt_payload(artifact=artifact)
    restored_schema = json.loads(json.dumps(restore["restored_schema"]))
    restored_schema["migrations"][-1]["checksum_sha256"] = "d" * 64
    restored_schema["fingerprint_sha256"] = dr._schema_ledger_fingerprint(restored_schema)
    restore["restored_schema"] = restored_schema
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(_backup_receipt_payload(artifact=artifact)), encoding="utf-8")
    restore_path.write_text(json.dumps(restore), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert exc.value.code == "schema_ledger_release_mismatch"


def test_release_gate_rejects_missing_source_schema_fingerprint(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup = _backup_receipt_payload(artifact=artifact)
    source_schema = dict(backup["source_schema"])
    source_schema.pop("fingerprint_sha256")
    backup["source_schema"] = source_schema
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    restore_path.write_text(json.dumps(_restore_receipt_payload(artifact=artifact)), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert exc.value.code == "schema_ledger_fingerprint_missing"


def test_release_gate_cli_fails_closed_and_writes_v2_failure_receipt(tmp_path: Path) -> None:
    receipt_path = tmp_path / "release-gate.failure.json"

    exit_code = dr.main(
        [
            "release-gate",
            "--backup-receipt",
            str(tmp_path / "missing-backup.json"),
            "--restore-receipt",
            str(tmp_path / "missing-restore.json"),
            "--release-commit-sha",
            RELEASE_COMMIT_SHA,
            "--image-digest",
            RELEASE_IMAGE_DIGEST,
            "--receipt",
            str(receipt_path),
        ]
    )

    assert exit_code == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == dr.RECEIPT_SCHEMA
    assert receipt["operation"] == "release_gate"
    assert receipt["status"] == "fail"
    assert receipt["error"]["code"] == "dr_receipt_invalid"
    assert receipt_path.stat().st_mode & 0o777 == 0o600


def test_atomic_receipt_replaces_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text("keep-me\n", encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.symlink_to(victim.name)

    dr._atomic_receipt(receipt_path, {"status": "pass"})

    assert victim.read_text(encoding="utf-8") == "keep-me\n"
    assert not receipt_path.is_symlink()
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == {
        "status": "pass"
    }
    assert receipt_path.stat().st_mode & 0o777 == 0o600


def test_atomic_receipt_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent.name, target_is_directory=True)

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._atomic_receipt(
            alias_parent / "receipt.json",
            {"status": "pass"},
        )

    assert exc.value.code == "receipt_destination_invalid"
    assert not (real_parent / "receipt.json").exists()


def test_atomic_receipt_rejects_untrusted_ancestor_before_creating_descendants(
    tmp_path: Path,
) -> None:
    untrusted = tmp_path / "untrusted"
    untrusted.mkdir()
    untrusted.chmod(0o777)
    descendant = untrusted / "must-not-be-created"
    try:
        with pytest.raises(dr.DisasterRecoveryError) as exc:
            dr._atomic_receipt(
                descendant / "receipt.json",
                {"status": "pass"},
            )
    finally:
        untrusted.chmod(0o700)

    assert exc.value.code == "receipt_destination_invalid"
    assert not descendant.exists()


def test_atomic_receipt_parent_swap_before_staging_cannot_redirect_or_clobber(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trusted_parent = tmp_path / "trusted"
    trusted_parent.mkdir()
    opened_parent = tmp_path / "opened-parent"
    attacker_parent = tmp_path / "attacker"
    attacker_parent.mkdir()
    attacker_receipt = attacker_parent / "receipt.json"
    attacker_receipt.write_text("keep-attacker-file\n", encoding="utf-8")
    receipt_path = trusted_parent / "receipt.json"
    swapped = False

    def swap_parent_before_staging(_length: int) -> str:
        nonlocal swapped
        if not swapped:
            trusted_parent.rename(opened_parent)
            attacker_parent.rename(trusted_parent)
            swapped = True
        return "d" * 32

    monkeypatch.setattr(dr.secrets, "token_hex", swap_parent_before_staging)

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._atomic_receipt(receipt_path, {"status": "pass"})

    assert swapped is True
    assert exc.value.code == "receipt_destination_invalid"
    assert receipt_path.read_text(encoding="utf-8") == "keep-attacker-file\n"
    assert list(opened_parent.iterdir()) == []


@pytest.mark.parametrize("operation", ("destination", "input"))
@pytest.mark.parametrize(
    ("fault_call", "fault_location"),
    ((1, "root"), (2, "nested")),
)
def test_receipt_directory_fstat_fault_closes_every_opened_fd_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    fault_call: int,
    fault_location: str,
) -> None:
    parent = tmp_path / "receipt-parent"
    parent.mkdir()
    receipt_path = parent / "backup.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema": dr.RECEIPT_SCHEMA,
                "status": "pass",
                "operation": "backup",
            }
        ),
        encoding="utf-8",
    )
    original_open = dr.os.open
    original_fstat = dr.os.fstat
    original_close = dr.os.close
    directory_flag = getattr(dr.os, "O_DIRECTORY", 0)
    opened_directory_fds: list[int] = []
    closed_directory_fds: list[int] = []
    directory_fstat_calls = 0

    def tracking_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        descriptor = original_open(path, flags, *args, **kwargs)
        if flags & directory_flag:
            opened_directory_fds.append(descriptor)
        return descriptor

    def faulting_fstat(descriptor: int):  # type: ignore[no-untyped-def]
        nonlocal directory_fstat_calls
        if descriptor in opened_directory_fds:
            directory_fstat_calls += 1
            if directory_fstat_calls == fault_call:
                raise OSError(f"injected {fault_location} directory fstat fault")
        return original_fstat(descriptor)

    def tracking_close(descriptor: int) -> None:
        if descriptor in opened_directory_fds:
            closed_directory_fds.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(dr.os, "open", tracking_open)
    monkeypatch.setattr(dr.os, "fstat", faulting_fstat)
    monkeypatch.setattr(dr.os, "close", tracking_close)

    if operation == "destination":
        with pytest.raises(OSError, match=f"injected {fault_location}"):
            dr._open_receipt_destination_directory(parent)
    else:
        with pytest.raises(dr.DisasterRecoveryError) as exc:
            dr._read_stable_receipt_json(
                receipt_path,
                code="dr_receipt_invalid",
                label="Backup receipt",
            )
        assert exc.value.code == "dr_receipt_invalid"

    assert directory_fstat_calls == fault_call
    assert closed_directory_fds == list(reversed(opened_directory_fds))
    assert len(closed_directory_fds) == len(set(closed_directory_fds))


def test_receipt_path_must_not_overwrite_an_input(tmp_path: Path) -> None:
    artifact = tmp_path / "backup.dump"

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._validate_receipt_destination(artifact, [artifact])

    assert exc.value.code == "receipt_path_conflict"


def test_canonical_queries_are_snapshot_bound_public_quoted_and_chunk_bounded() -> None:
    commands: list[dict[str, object]] = []

    def runner(command, **_kwargs):
        command = list(command)
        sql = command[command.index("--command") + 1]
        return _result(stdout="BEGIN\nSET\nSET\n" + _critical_query_result(sql))

    evidence = dr._critical_data_evidence_from_database(
        label="Source",
        step="source_critical_data",
        psql="/mock/bin/psql",
        database_url="postgresql://owner@db.example/propertyquarry",
        env={},
        runner=runner,
        commands=commands,
        snapshot_id="00000003-0000001B-1",
    )

    assert evidence == _critical_data_evidence()
    assert len(commands) == 2 * len(dr.CRITICAL_DATA_TABLES)
    snapshot_prefix = (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
        "SET TRANSACTION SNAPSHOT '00000003-0000001B-1'; "
    )
    for index, (table, identity_columns, _required) in enumerate(dr.CRITICAL_DATA_TABLES):
        preflight_sql = str(commands[index * 2]["command"][-1])
        full_sql = str(commands[(index * 2) + 1]["command"][-1])
        assert preflight_sql.startswith(snapshot_prefix)
        assert full_sql.startswith(snapshot_prefix)
        assert f"propertyquarry_critical_data:{table}:row_bound_preflight" in preflight_sql
        assert f'FROM "public"."{table}" AS source_row' in preflight_sql
        assert f"LIMIT {dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS + 1}" in preflight_sql
        for expensive_operation in (
            "to_jsonb",
            "row_number",
            "ORDER BY",
            "sha256",
            "json_build_object",
            "canonical_rows",
        ):
            assert expensive_operation not in preflight_sql

        assert f'FROM "public"."{table}" AS source_row' in full_sql
        assert "bounded_source AS MATERIALIZED" in full_sql
        assert f"LIMIT {dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS}" in full_sql
        for column in identity_columns:
            assert f'source_row."{column}"' in full_sql
            assert f'digested_row."{column}"' in full_sql
        assert 'digested_row.row_sha256 COLLATE "C"' in full_sql
        assert "bounded_chunks AS MATERIALIZED" in full_sql
        assert f"((ordinal - 1) / {dr.CRITICAL_DATA_CHUNK_SIZE})" in full_sql
        assert f"row_size_bytes <= {dr.CRITICAL_DATA_MAX_ROW_BYTES}" in full_sql
        assert f"ordinal <= {dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS}" in full_sql
        assert "string_agg(row_sha256, '' ORDER BY ordinal)" in full_sql
        assert "all_chunks" not in full_sql
        assert f"FROM {table} " not in preflight_sql
        assert f"FROM {table} " not in full_sql


def test_over_limit_preflight_never_executes_full_merkle_query() -> None:
    commands: list[dict[str, object]] = []

    def runner(command, **_kwargs):
        command = list(command)
        sql = command[command.index("--command") + 1]
        assert "row_bound_preflight" in sql
        return _result(
            stdout="BEGIN\nSET\nSET\n"
            f"{dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS + 1}\n"
        )

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._critical_data_evidence_from_database(
            label="Source",
            step="source_critical_data",
            psql="/mock/bin/psql",
            database_url="postgresql://owner@db.example/propertyquarry",
            env={},
            runner=runner,
            commands=commands,
            snapshot_id="00000003-0000001B-1",
        )

    assert exc.value.code == "critical_data_scale_bound_exceeded"
    assert len(commands) == 1
    preflight_sql = str(commands[0]["command"][-1])
    assert preflight_sql.startswith(
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
        "SET TRANSACTION SNAPSHOT '00000003-0000001B-1'; "
    )
    assert f'FROM "public"."{dr.CRITICAL_DATA_TABLES[0][0]}" AS source_row' in preflight_sql
    assert f"LIMIT {dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS + 1}" in preflight_sql
    assert "to_jsonb" not in preflight_sql
    assert "ORDER BY" not in preflight_sql
    assert "sha256" not in preflight_sql


def test_full_merkle_query_must_match_same_snapshot_preflight_count() -> None:
    commands: list[dict[str, object]] = []

    def runner(command, **_kwargs):
        command = list(command)
        sql = command[command.index("--command") + 1]
        if "row_bound_preflight" in sql:
            return _result(stdout="BEGIN\nSET\nSET\n3\n")
        observed = json.loads(_critical_query_result(sql))
        observed["row_count"] = 2
        return _result(stdout="BEGIN\nSET\nSET\n" + json.dumps(observed) + "\n")

    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._critical_data_evidence_from_database(
            label="Source",
            step="source_critical_data",
            psql="/mock/bin/psql",
            database_url="postgresql://owner@db.example/propertyquarry",
            env={},
            runner=runner,
            commands=commands,
            snapshot_id="00000003-0000001B-1",
        )

    assert exc.value.code == "critical_data_query_invalid"
    assert len(commands) == 2
    assert all(
        str(command["command"][-1]).startswith(
            "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
            "SET TRANSACTION SNAPSHOT '00000003-0000001B-1'; "
        )
        for command in commands
    )


def test_schema_ledger_queries_are_snapshot_bound_to_quoted_public_table() -> None:
    commands: list[dict[str, object]] = []

    def runner(command, **_kwargs):
        sql = list(command)[list(command).index("--command") + 1]
        if "to_regclass" in sql:
            return _result(stdout="BEGIN\nSET\nt\n")
        return _result(
            stdout="BEGIN\nSET\n" + json.dumps(_schema_ledger()["migrations"]) + "\n"
        )

    evidence = dr._migration_ledger_from_database(
        label="Source",
        step="source_migration_ledger",
        psql="/mock/bin/psql",
        database_url="postgresql://owner@db.example/propertyquarry",
        env={},
        runner=runner,
        commands=commands,
        require_current=True,
        snapshot_id="00000003-0000001B-1",
    )

    assert evidence == _schema_ledger()
    assert len(commands) == 2
    for command_receipt in commands:
        sql = str(command_receipt["command"][-1])
        assert sql.startswith(
            "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
            "SET TRANSACTION SNAPSHOT '00000003-0000001B-1'; "
        )
    ledger_table = str(_schema_ledger()["ledger_table"])
    assert f'''to_regclass('"public"."{ledger_table}"')''' in str(
        commands[0]["command"][-1]
    )
    assert f'FROM "public"."{ledger_table}" AS ledger_row' in str(
        commands[1]["command"][-1]
    )
    assert f"FROM {ledger_table} " not in str(commands[1]["command"][-1])


def test_merkle_evidence_scales_past_sixteen_point_seven_million_rows() -> None:
    row_count = 16_777_217
    evidence = _critical_data_evidence(row_counts={"property_search_runs": row_count})

    normalized = dr._validated_critical_data_evidence(evidence, label="Large source")
    search_runs = normalized["tables"][0]

    assert search_runs["row_count"] == row_count
    assert search_runs["chunk_count"] == 16_385
    assert search_runs["chunk_count"] < dr.CRITICAL_DATA_MAX_CHUNKS
    assert search_runs["chunks"][-1]["row_count"] == 1
    assert search_runs["merkle_root_sha256"] == dr._critical_merkle_root(
        "property_search_runs",
        search_runs["chunks"],
    )


def test_merkle_evidence_rejects_chunk_tamper_and_contract_scale_overflow() -> None:
    tampered = json.loads(json.dumps(_critical_data_evidence()))
    tampered["tables"][0]["chunks"][0]["chunk_sha256"] = "f" * 64

    with pytest.raises(dr.DisasterRecoveryError) as tampered_exc:
        dr._validated_critical_data_evidence(tampered, label="Tampered source")

    assert tampered_exc.value.code == "critical_data_fingerprint_invalid"

    overflow = json.loads(json.dumps(_critical_data_evidence()))
    overflow["tables"][0]["row_count"] = dr.CRITICAL_DATA_MAX_SUPPORTED_ROWS + 1
    overflow["tables"][0]["chunk_count"] = dr.CRITICAL_DATA_MAX_CHUNKS + 1

    with pytest.raises(dr.DisasterRecoveryError) as overflow_exc:
        dr._validated_critical_data_evidence(overflow, label="Oversized source")

    assert overflow_exc.value.code == "critical_data_scale_bound_exceeded"


def test_release_gate_rejects_missing_or_changed_exported_snapshot_binding(tmp_path: Path) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"encrypted-release-backup")
    backup = _backup_receipt_payload(artifact=artifact)
    restore = _restore_receipt_payload(artifact=artifact)
    backup.pop("source_snapshot")
    backup_path = tmp_path / "backup.json"
    restore_path = tmp_path / "restore.json"
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    restore_path.write_text(json.dumps(restore), encoding="utf-8")
    now = datetime(2026, 7, 13, 10, 0, 30, tzinfo=timezone.utc).timestamp()

    with pytest.raises(dr.DisasterRecoveryError) as missing_exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert missing_exc.value.code == "snapshot_evidence_missing"

    plaintext_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    backup["source_snapshot"] = _source_snapshot_evidence(
        plaintext_sha256=plaintext_sha256
    )
    changed_snapshot = dr._snapshot_evidence(
        {
            "snapshot_id_sha256": "f" * 64,
            "transaction_snapshot_sha256": hashlib.sha256(b"100:200:150").hexdigest(),
        },
        pg_dump_plaintext_sha256=plaintext_sha256,
    )
    restore["source_snapshot"] = changed_snapshot
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    restore_path.write_text(json.dumps(restore), encoding="utf-8")

    with pytest.raises(dr.DisasterRecoveryError) as changed_exc:
        dr.verify_release_dr_evidence(
            backup_receipt_path=backup_path,
            restore_receipt_path=restore_path,
            release_commit_sha=RELEASE_COMMIT_SHA,
            image_digest=RELEASE_IMAGE_DIGEST,
            max_age_seconds=60,
            clock=_Clock(now),
        )

    assert changed_exc.value.code == "release_snapshot_mismatch"


def test_snapshot_import_rejects_injected_or_unexported_identity() -> None:
    with pytest.raises(dr.DisasterRecoveryError) as exc:
        dr._snapshot_bound_sql(
            "SELECT 1;",
            "00000003-0000001B-1'; DROP TABLE public.property_search_runs; --",
        )

    assert exc.value.code == "snapshot_identity_invalid"
