from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from scripts import propertyquarry_live_external_authority_probe as probe


def _result(*, stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _docker_row(*, container: str, environment: dict[str, str]) -> str:
    return json.dumps(
        [
            {
                "Name": container,
                "State": {"Status": "running", "Health": {"Status": "healthy"}},
                "Config": {"Env": [f"{key}={value}" for key, value in environment.items()]},
            }
        ]
    )


def _trusted(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o600)


def _postgres_client_pin(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    binaries: dict[str, Path] = {}
    rows: dict[str, dict[str, str]] = {}
    for name in probe.POSTGRES_CLIENT_BINARIES:
        binary = tmp_path / "postgres-client" / "bin" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{name} (PostgreSQL) 16.14'\n",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        binaries[name] = binary
        rows[name] = {
            "path": str(binary),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
    pin = tmp_path / "postgres-client-release-pin.json"
    pin.write_text(
        json.dumps(
            {
                "binaries": rows,
                "package": {
                    "name": "postgresql-client-16",
                    "sha256": "c" * 64,
                    "version": "16.14-0ubuntu0.24.04.1",
                },
                "schema": probe.POSTGRES_CLIENT_RELEASE_PIN_SCHEMA,
                "status": "CONFIGURED",
                "version": "16.14",
            }
        ),
        encoding="utf-8",
    )
    pin.chmod(0o600)
    return pin, binaries


def test_probe_reports_only_named_external_authority_blockers(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        command = list(command)
        if command[:3] == ["docker", "container", "inspect"]:
            return _result(stdout=_docker_row(container=command[-1], environment={}))
        if "--list-keys" in command:
            return _result(stdout="tru::1:0:3:1:5\n")
        return _result(returncode=1)

    receipt = probe.build_live_external_authority_receipt(
        environ={},
        runner=runner,
        clock=lambda: 1_786_604_800.0,
        which=lambda name: "/usr/bin/gpg" if name == "gpg" else None,
        attestor=lambda **_kwargs: {
            "version": "2.35.16",
            "sha256": "a" * 64,
            "path": "/trusted/aws",
        },
        cli_runner=lambda **_kwargs: (_ for _ in ()).throw(
            probe.DisasterRecoveryError("no_credentials", "no credentials")
        ),
        global_trust_store=tmp_path / "missing-trust.json",
        public_launch_authority=tmp_path / "missing-authority.json",
        postgres_client_release_pin=tmp_path / "missing-postgres-pin.json",
    )

    assert receipt["status"] == "external_authority_required"
    assert receipt["dr"]["approved_recipient"]["public_recipient_count"] == 0
    assert receipt["dr"]["aws"]["caller_identity_verified"] is False
    assert receipt["billing"]["must_remain_fail_closed"] is True
    assert receipt["fliplink"]["required_customer_label"] == "local_only"
    assert receipt["blockers"] == [
        "approved_external_encryption_recipient",
        "scoped_aws_identity",
        "versioned_compliance_locked_s3_target",
        "local_postgres_restore_toolchain",
        "disposable_restore_target",
        "signed_public_launch_authority",
        "same_principal_live_billing_authority",
        "fliplink_external_publication_authority",
    ]
    assert receipt["secret_values_recorded"] is False
    assert '"environment":' not in json.dumps(receipt).lower()


def test_probe_admits_complete_authority_without_recording_values(tmp_path: Path) -> None:
    postgres_pin, _binaries = _postgres_client_pin(tmp_path)
    api_environment = {name: "configured-secret-value" for name in (*probe.BILLING_KEYS, *probe.FLIPLINK_KEYS)}
    backup_environment = {
        name: "configured-secret-value"
        for name in (*probe.DR_PROVIDER_KEYS, *probe.DR_RECOVERY_KEYS)
    }

    def runner(command, **_kwargs):
        command = list(command)
        if command[:3] == ["docker", "container", "inspect"]:
            environment = api_environment if command[-1] == probe.API_CONTAINER else backup_environment
            return _result(stdout=_docker_row(container=command[-1], environment=environment))
        if "--list-keys" in command:
            return _result(stdout="pub:-:4096:1:REDACTED:0:0::::::23::0:\n")
        if command[-1:] == ["--version"]:
            return _result(
                stdout=f"{Path(command[0]).name} (PostgreSQL) 16.14\n"
            )
        return _result()

    trust = tmp_path / "trust.json"
    authority = tmp_path / "authority.json"
    _trusted(trust)
    _trusted(authority)
    receipt = probe.build_live_external_authority_receipt(
        environ={},
        runner=runner,
        which=lambda name: f"/usr/bin/{name}",
        attestor=lambda **_kwargs: {
            "version": "2.35.16",
            "sha256": "b" * 64,
            "path": "/trusted/aws",
        },
        cli_runner=lambda **_kwargs: _result(
            stdout=json.dumps({"Account": "redacted", "Arn": "redacted", "UserId": "redacted"})
        ),
        global_trust_store=trust,
        public_launch_authority=authority,
        authority_validator=lambda path: path in {trust, authority},
        postgres_client_release_pin=postgres_pin,
    )

    assert receipt["status"] == "ready"
    assert receipt["blockers"] == []
    assert receipt["dr"]["s3_target"]["ready"] is True
    assert receipt["dr"]["restore"]["ready"] is True
    assert receipt["dr"]["restore"]["postgres_client"]["release_pin_attested"] is True
    assert receipt["billing"]["safe_handoff_ready"] is True
    assert receipt["fliplink"]["external_publication_ready"] is True
    serialized = json.dumps(receipt)
    assert "configured-secret-value" not in serialized
    assert "redacted" not in serialized


def test_postgres_client_pin_rejects_binary_hash_drift(tmp_path: Path) -> None:
    postgres_pin, binaries = _postgres_client_pin(tmp_path)
    binaries["pg_restore"].write_text("changed\n", encoding="utf-8")
    binaries["pg_restore"].chmod(0o700)

    projection = probe._postgres_client_release_pin_projection(
        postgres_pin,
        runner=lambda *_args, **_kwargs: _result(
            stdout="tool (PostgreSQL) 16.14\n"
        ),
    )

    assert projection["ready"] is False
    assert projection["release_pin_attested"] is False
    assert projection["state"] == "pg_restore_attestation_failed"
    assert "path" not in json.dumps(projection).lower()


def test_postgres_live_client_call_is_real_secret_safe_and_ephemeral(
    tmp_path: Path,
) -> None:
    postgres_pin, _binaries = _postgres_client_pin(tmp_path)
    observed_passwords: list[str] = []
    archive_paths: list[Path] = []

    def runner(command, **kwargs):
        command = list(command)
        name = Path(command[0]).name
        if command[:3] == ["docker", "container", "inspect"]:
            return _result(
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Env": [
                                    "POSTGRES_DB=propertyquarry",
                                    "POSTGRES_PASSWORD=database-secret-value",
                                ]
                            },
                            "NetworkSettings": {
                                "Networks": {
                                    "property_default": {"IPAddress": "192.0.2.10"}
                                }
                            },
                        }
                    ]
                )
            )
        if command[-1:] == ["--version"]:
            return _result(stdout=f"{name} (PostgreSQL) 16.14\n")
        observed_passwords.append(str(dict(kwargs["env"]).get("PGPASSWORD") or ""))
        if name == "psql":
            return _result(stdout="1|160014\n")
        if name == "pg_dump":
            archive = Path(
                next(
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--file=")
                )
            )
            archive.write_bytes(b"private-custom-archive")
            archive.chmod(0o600)
            archive_paths.append(archive)
            return _result()
        if name == "pg_restore":
            return _result(
                stdout=(
                    "1; 0 0 TABLE public example owner\n"
                    "2; 0 0 TABLE DATA public example owner\n"
                )
            )
        return _result(returncode=1)

    projection = probe._postgres_live_client_call_projection(
        postgres_pin,
        runner=runner,
    )

    assert projection["state"] == "verified"
    assert projection["live_database_readback"] is True
    assert projection["server_version_num"] == "160014"
    assert projection["archive_entries"] == 2
    assert projection["archive_bytes"] == len(b"private-custom-archive")
    assert projection["archive_sha256"] == hashlib.sha256(
        b"private-custom-archive"
    ).hexdigest()
    assert projection["plaintext_archive_retained"] is False
    assert projection["passing_off_host_dr_claim"] is False
    assert projection["secret_values_recorded"] is False
    assert observed_passwords == [
        "database-secret-value",
        "database-secret-value",
        "",
    ]
    assert archive_paths and all(not path.exists() for path in archive_paths)
    assert "database-secret-value" not in json.dumps(projection)


def test_external_billing_candidate_is_live_probed_but_never_authorized(
    tmp_path: Path,
) -> None:
    ea_environment = {
        "PAYPAL_CLIENT_ID": "paypal-client-secret-value",
        "PAYPAL_SECRET": "paypal-secret-value",
        "PAYPAL_ACCOUNT_EMAIL": "private-account@example.test",
        "PAYFUNNELS_WEBHOOK_SECRET": "payfunnels-secret-value",
        "EA_MEMORIAL_FLIPLINK_WEBHOOK_SECRET": "memorial-secret-value",
    }

    def runner(command, **_kwargs):
        command = list(command)
        if command[:3] == ["docker", "container", "inspect"]:
            environment = ea_environment if command[-1] == probe.EA_API_CONTAINER else {}
            return _result(
                stdout=_docker_row(container=command[-1], environment=environment)
            )
        if command[:3] == ["docker", "exec", "-i"]:
            return _result(
                stdout=json.dumps(
                    {
                        "access_token_verified": True,
                        "api_environment": "live",
                        "classification_probe_attempted": True,
                        "classified_token_http_status": 200,
                        "credential_environment": "sandbox",
                        "state": "sandbox_credentials_verified",
                        "token_http_status": 401,
                        "webhook_count": 0,
                        "webhook_list_http_status": 200,
                    }
                )
            )
        if "--list-keys" in command:
            return _result()
        return _result(returncode=1)

    receipt = probe.build_live_external_authority_receipt(
        environ={},
        runner=runner,
        which=lambda name: "/usr/bin/gpg" if name == "gpg" else None,
        attestor=lambda **_kwargs: {
            "version": "2.35.16",
            "sha256": "d" * 64,
            "path": "/trusted/aws",
        },
        cli_runner=lambda **_kwargs: (_ for _ in ()).throw(
            probe.DisasterRecoveryError("no_credentials", "no credentials")
        ),
        global_trust_store=tmp_path / "missing-trust.json",
        public_launch_authority=tmp_path / "missing-authority.json",
        postgres_client_release_pin=tmp_path / "missing-postgres-pin.json",
        probe_external_billing=True,
    )

    paypal = receipt["billing"]["external_candidate"]["paypal"]
    payfunnels = receipt["billing"]["external_candidate"]["payfunnels"]
    fliplink = receipt["fliplink"]["external_candidate"]
    assert paypal["client_credentials_configured"] is True
    assert paypal["state"] == "sandbox_credentials_verified"
    assert paypal["token_http_status"] == 401
    assert paypal["classified_token_http_status"] == 200
    assert paypal["credential_environment"] == "sandbox"
    assert paypal["access_token_verified"] is True
    assert paypal["propertyquarry_principal_authorized"] is False
    assert paypal["billing_enabled"] is False
    assert payfunnels["webhook_secret_configured"] is True
    assert payfunnels["api_key_configured"] is False
    assert payfunnels["propertyquarry_principal_authorized"] is False
    assert fliplink["memorial_webhook_secret_configured"] is True
    assert fliplink["propertyquarry_credential_count"] == 0
    assert fliplink["propertyquarry_principal_authorized"] is False
    serialized = json.dumps(receipt)
    for secret in ea_environment.values():
        assert secret not in serialized


def test_private_writer_materializes_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "private" / "receipt.json"
    probe._write_private_json(target, {"schema": probe.SCHEMA, "status": "test"})

    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == probe.SCHEMA
