from __future__ import annotations

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
        "disposable_restore_target_and_toolchain",
        "signed_public_launch_authority",
        "same_principal_live_billing_authority",
        "fliplink_external_publication_authority",
    ]
    assert receipt["secret_values_recorded"] is False
    assert "environment" not in json.dumps(receipt).lower()


def test_probe_admits_complete_authority_without_recording_values(tmp_path: Path) -> None:
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
    )

    assert receipt["status"] == "ready"
    assert receipt["blockers"] == []
    assert receipt["dr"]["s3_target"]["ready"] is True
    assert receipt["dr"]["restore"]["ready"] is True
    assert receipt["billing"]["safe_handoff_ready"] is True
    assert receipt["fliplink"]["external_publication_ready"] is True
    serialized = json.dumps(receipt)
    assert "configured-secret-value" not in serialized
    assert "redacted" not in serialized


def test_private_writer_materializes_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "private" / "receipt.json"
    probe._write_private_json(target, {"schema": probe.SCHEMA, "status": "test"})

    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == probe.SCHEMA
