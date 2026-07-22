from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from scripts import propertyquarry_release_installation_model as installation


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packaging" / "propertyquarry-release-control-v2"
SYSTEMD = PACKAGE / "systemd"
SCHEMA = PACKAGE / "schema"


def _read(relative: str) -> str:
    return (PACKAGE / relative).read_text(encoding="utf-8")


def _schema(name: str) -> dict[str, object]:
    value = json.loads((SCHEMA / name).read_text(encoding="utf-8"))
    assert type(value) is dict
    Draft202012Validator.check_schema(value)
    return value


def _controller_config() -> dict[str, object]:
    mediator = lambda name: {  # noqa: E731
        "endpoint": f"https://{name}.mediator.invalid/v2",
        "trust_root_path": (
            "/etc/propertyquarry-release-control/trust.d/"
            "resource-mediator-v2.pem"
        ),
        "credential_name": "resource-mediator-client",
    }
    return {
        "schema": "propertyquarry.release-control.controller-config.v2",
        "version": 2,
        "environment": "propertyquarry-production",
        "identity_policy": {
            "repository": "ArchonMegalon/propertyquarry",
            "ref": "refs/heads/main",
            "workflow_ref": (
                "ArchonMegalon/propertyquarry/.github/workflows/"
                "smoke-runtime.yml@refs/heads/main"
            ),
            "workflow_sha": "a" * 40,
            "job": "propertyquarry-release-v2",
            "environment": "propertyquarry-production",
        },
        "oidc": {
            "allowed_request_url_origin": (
                "https://vstoken.actions.githubusercontent.com"
            ),
            "audience": "propertyquarry-release-control-v2",
        },
        "root_policy_path": "/etc/propertyquarry-release-control/policy-v2.json",
        "package_trust_root_path": (
            "/etc/propertyquarry-release-control/trust.d/package-authority-v2.pem"
        ),
        "authorities": {
            "request": {
                "endpoint": "https://request-authority.invalid/v2",
                "request_trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "request-authority-v2.pem"
                ),
                "response_trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "response-authority-v2.pem"
                ),
                "credential_name": "request-authority-client",
            },
            "lifecycle_cas": {
                "endpoint": "https://lifecycle-cas.invalid/v2",
                "trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "lifecycle-cas-v2.pem"
                ),
                "credential_name": "lifecycle-cas-client",
            },
            "evidence_store": {
                "endpoint": "https://evidence.invalid/v2",
                "trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "evidence-authority-v2.pem"
                ),
                "credential_name": "evidence-store-client",
            },
        },
        "resource_mediators": {
            "database": mediator("database"),
            "launch_authority": mediator("launch-authority"),
            "monitoring_delivery": mediator("monitoring-delivery"),
            "overlay": mediator("overlay"),
            "public_tour": mediator("public-tour"),
            "runtime": mediator("runtime"),
            "traffic": mediator("traffic"),
        },
        "state": {
            "cache_path": "/var/lib/propertyquarry-release-control-v2/cache",
            "journal_path": "/var/lib/propertyquarry-release-control-v2/journal",
            "receipt_path": "/var/lib/propertyquarry-release-control-v2/receipts",
        },
        "limits": {
            "identity_token_limit_bytes": 16384,
            "request_limit_bytes": 1048576,
            "response_limit_bytes": 1048576,
            "diagnostic_limit_bytes": 65536,
            "callback_timeout_seconds": 30,
            "operation_timeout_seconds": 9600,
        },
    }


def _watchdog_config() -> dict[str, object]:
    return {
        "schema": "propertyquarry.release-control.watchdog-config.v2",
        "version": 2,
        "environment": "propertyquarry-production",
        "root_policy_path": "/etc/propertyquarry-release-control/policy-v2.json",
        "lifecycle_cas": {
            "endpoint": "https://lifecycle-cas.invalid/v2",
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/lifecycle-cas-v2.pem"
            ),
            "credential_name": "watchdog-takeover-client",
        },
        "resource_recovery": {
            "endpoint": "https://resource-recovery.invalid/v2",
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/"
                "resource-mediator-v2.pem"
            ),
            "credential_name": "resource-recovery-client",
        },
        "resource_kinds": [
            "database",
            "launch-authority",
            "monitoring-delivery",
            "overlay",
            "public-tour",
            "runtime",
            "traffic",
        ],
        "state": {
            "cache_path": "/var/lib/propertyquarry-release-watchdog-v2/cache"
        },
        "limits": {
            "poll_interval_seconds": 10,
            "callback_timeout_seconds": 30,
            "reconciliation_timeout_seconds": 1800,
        },
    }


def _validate(schema: dict[str, object], value: object) -> None:
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(value)


def test_source_package_is_templates_only_and_contains_no_authority_binary() -> None:
    expected = {
        "systemd/propertyquarry-release-control-v2.socket",
        "systemd/propertyquarry-release-control-v2@.service",
        "systemd/propertyquarry-release-watchdog-v2.service",
        "sysusers.d/propertyquarry-release-control-v2.conf",
        "tmpfiles.d/propertyquarry-release-control-v2.conf",
        "schema/controller-v2.schema.json",
        "schema/watchdog-v2.schema.json",
    }
    actual = {
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert not (PACKAGE / "bin").exists()
    assert not (PACKAGE / "usr" / "libexec").exists()
    source_to_role = {
        "systemd/propertyquarry-release-control-v2.socket": "systemd-socket-unit",
        "systemd/propertyquarry-release-control-v2@.service": (
            "systemd-controller-template-unit"
        ),
        "systemd/propertyquarry-release-watchdog-v2.service": (
            "systemd-watchdog-unit"
        ),
        "sysusers.d/propertyquarry-release-control-v2.conf": "sysusers-config",
        "tmpfiles.d/propertyquarry-release-control-v2.conf": "tmpfiles-config",
        "schema/controller-v2.schema.json": "controller-schema",
        "schema/watchdog-v2.schema.json": "watchdog-schema",
    }
    for source, role in source_to_role.items():
        assert source in actual
        assert installation.ROLE_BY_NAME[role].path.startswith("/")


def test_socket_is_single_connection_peer_credential_gate() -> None:
    socket = _read("systemd/propertyquarry-release-control-v2.socket")
    for exact in (
        "ListenStream=/run/propertyquarry-release-control-v2/request.sock",
        "SocketUser=root",
        "SocketGroup=propertyquarry-release-callers",
        "SocketMode=0660",
        "DirectoryMode=0750",
        "Accept=yes",
        "PassCredentials=yes",
        "Backlog=1",
        "MaxConnections=1",
        "RemoveOnStop=yes",
    ):
        assert socket.count(exact) == 1
    assert "MaxConnectionsPerSource=" not in socket
    assert "FlushPending=" not in socket
    assert "Service=" not in socket
    assert "ListenDatagram=" not in socket
    assert "SocketMode=0666" not in socket


@pytest.mark.parametrize(
    ("name", "user", "state", "executable"),
    [
        (
            "propertyquarry-release-control-v2@.service",
            "propertyquarry-release-control",
            "propertyquarry-release-control-v2",
            "propertyquarry-release-supervisor-v2",
        ),
        (
            "propertyquarry-release-watchdog-v2.service",
            "propertyquarry-release-watchdog",
            "propertyquarry-release-watchdog-v2",
            "propertyquarry-release-watchdog-v2",
        ),
    ],
)
def test_services_have_escape_proof_cgroup_and_os_sandbox_contract(
    name: str, user: str, state: str, executable: str
) -> None:
    unit = _read(f"systemd/{name}")
    assert f"User={user}" in unit
    assert f"StateDirectory={state}" in unit
    assert (
        "/usr/libexec/propertyquarry-release-control/" + executable
    ) in unit
    for exact in (
        "ExitType=cgroup",
        "CollectMode=inactive-or-failed",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "FinalKillSignal=SIGKILL",
        "Delegate=no",
        "OOMPolicy=kill",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectControlGroups=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "RestrictNamespaces=yes",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "DevicePolicy=closed",
        "SystemCallArchitectures=native",
        "StandardOutput=journal",
        "StandardError=journal",
        "LogRateLimitIntervalSec=30",
        "LogRateLimitBurst=50",
        "UMask=0077",
    ):
        assert unit.count(exact) == 1
    assert unit.count("LimitCORE=0") == 1
    for forbidden in (
        "EnvironmentFile=",
        "StandardOutput=socket",
        "StandardError=inherit",
        "ExecStart=/bin/",
        "ExecStart=/usr/bin/python",
        "ExecStart=/usr/bin/bash",
        "ExecStart=/bin/sh",
        "sudo",
        "/docker/property",
        "GITHUB_WORKSPACE}",
        "Delegate=yes",
        "KillMode=process",
        "CapabilityBoundingSet=CAP_",
        "AmbientCapabilities=CAP_",
    ):
        assert forbidden not in unit
    assert not any(
        line.startswith("Environment=") for line in unit.splitlines()
    )
    unset = next(
        line for line in unit.splitlines() if line.startswith("UnsetEnvironment=")
    )
    for name in (
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "PROPERTYQUARRY_OIDC_TOKEN_FD",
        "GITHUB_TOKEN",
        "GITHUB_WORKSPACE",
        "RUNNER_TEMP",
        "HTTP_PROXY",
        "http_proxy",
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "RUST_LOG",
        "GODEBUG",
    ):
        assert name in unset


def test_request_broker_is_only_socket_activated_and_watchdog_is_separate() -> None:
    controller = _read("systemd/propertyquarry-release-control-v2@.service")
    watchdog = _read("systemd/propertyquarry-release-watchdog-v2.service")
    assert "RefuseManualStart=yes" in controller
    assert "StandardInput=socket" in controller
    assert (
        "ExecStart=/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-supervisor-v2 --server-broker --config "
        "/etc/propertyquarry-release-control/controller-v2.json "
        "--socket-activation"
    ) in controller
    assert "propertyquarry-release-controller-v2 --socket-activation" not in controller
    assert "--socket-activation" in controller
    assert "[Install]" not in controller
    assert "Restart=no" in controller
    assert "RuntimeMaxSec=9900" in controller
    controller_schema = _schema("controller-v2.schema.json")
    worker_maximum = controller_schema["properties"]["limits"]["properties"][
        "operation_timeout_seconds"
    ]["maximum"]
    assert type(worker_maximum) is int
    assert worker_maximum == 9600
    assert worker_maximum < 9900
    assert "User=propertyquarry-release-watchdog" not in controller
    assert "Type=notify" in watchdog
    assert "WatchdogSec=30" in watchdog
    assert "Restart=always" in watchdog
    assert "User=propertyquarry-release-watchdog" in watchdog
    assert "StateDirectory=propertyquarry-release-watchdog-v2" in watchdog
    assert "request-authority-client" not in watchdog
    assert "resource-mediator-client" not in watchdog
    assert "watchdog-takeover-client" in watchdog
    assert "resource-recovery-client" in watchdog


def test_sysusers_and_tmpfiles_do_not_grant_candidate_or_caller_state_access() -> None:
    sysusers = _read("sysusers.d/propertyquarry-release-control-v2.conf")
    assert sysusers.splitlines() == [
        "g propertyquarry-release-callers -",
        (
            'u propertyquarry-release-control - "PropertyQuarry release '
            'controller" /var/lib/propertyquarry-release-control-v2 '
            "/usr/sbin/nologin"
        ),
        (
            'u propertyquarry-release-watchdog - "PropertyQuarry release '
            'watchdog" /var/lib/propertyquarry-release-watchdog-v2 '
            "/usr/sbin/nologin"
        ),
    ]
    assert "github" not in sysusers.lower()
    assert "runner" not in sysusers.lower()
    tmpfiles = _read("tmpfiles.d/propertyquarry-release-control-v2.conf")
    expected = {
        "d /etc/propertyquarry-release-control 0750 root propertyquarry-release-control - -",
        "d /etc/propertyquarry-release-control/trust.d 0750 root propertyquarry-release-control - -",
        "d /run/propertyquarry-release-control-v2 0750 root propertyquarry-release-callers - -",
        "d /var/lib/propertyquarry-release-control-v2 0700 propertyquarry-release-control propertyquarry-release-control - -",
        "d /var/lib/propertyquarry-release-control-v2/cache 0700 propertyquarry-release-control propertyquarry-release-control - -",
        "d /var/lib/propertyquarry-release-control-v2/journal 0700 propertyquarry-release-control propertyquarry-release-control - -",
        "d /var/lib/propertyquarry-release-control-v2/receipts 0700 propertyquarry-release-control propertyquarry-release-control - -",
        "d /var/lib/propertyquarry-release-watchdog-v2 0700 propertyquarry-release-watchdog propertyquarry-release-watchdog - -",
        "d /var/lib/propertyquarry-release-watchdog-v2/cache 0700 propertyquarry-release-watchdog propertyquarry-release-watchdog - -",
    }
    assert set(tmpfiles.splitlines()) == expected
    assert " 077" not in tmpfiles
    assert "credstore" not in tmpfiles


def test_closed_controller_and_watchdog_configs_validate() -> None:
    _validate(_schema("controller-v2.schema.json"), _controller_config())
    _validate(_schema("watchdog-v2.schema.json"), _watchdog_config())


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "bool-version",
        "http-endpoint",
        "userinfo-endpoint",
        "query-endpoint",
        "empty-authority-endpoint",
        "fragment-endpoint",
        "malformed-host-endpoint",
        "custom-port-endpoint",
        "missing-resource",
        "raw-secret",
        "candidate-policy-path",
        "credential-alias",
    ],
)
def test_controller_schema_rejects_hostile_config_mutations(mutation: str) -> None:
    value = _controller_config()
    if mutation == "extra":
        value["candidate_extension"] = True
    elif mutation == "bool-version":
        value["version"] = True
    elif mutation == "http-endpoint":
        value["authorities"]["request"]["endpoint"] = "http://authority.invalid"  # type: ignore[index]
    elif mutation == "userinfo-endpoint":
        value["authorities"]["request"]["endpoint"] = (  # type: ignore[index]
            "https://user:password@authority.invalid/v2"
        )
    elif mutation == "query-endpoint":
        value["authorities"]["request"]["endpoint"] = (  # type: ignore[index]
            "https://authority.invalid/v2?token=secret"
        )
    elif mutation == "empty-authority-endpoint":
        value["authorities"]["request"]["endpoint"] = "https:///v2"  # type: ignore[index]
    elif mutation == "fragment-endpoint":
        value["authorities"]["request"]["endpoint"] = (  # type: ignore[index]
            "https://authority.invalid/v2#alternate"
        )
    elif mutation == "malformed-host-endpoint":
        value["authorities"]["request"]["endpoint"] = (  # type: ignore[index]
            "https://-authority.invalid/v2"
        )
    elif mutation == "custom-port-endpoint":
        value["authorities"]["request"]["endpoint"] = (  # type: ignore[index]
            "https://authority.invalid:8443/v2"
        )
    elif mutation == "missing-resource":
        del value["resource_mediators"]["traffic"]  # type: ignore[index]
    elif mutation == "raw-secret":
        value["authorities"]["request"]["token"] = "secret"  # type: ignore[index]
    elif mutation == "candidate-policy-path":
        value["root_policy_path"] = "/docker/property/policy.json"
    else:
        value["authorities"]["lifecycle_cas"]["credential_name"] = "other"  # type: ignore[index]
    with pytest.raises(ValidationError):
        _validate(_schema("controller-v2.schema.json"), value)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "wrong-resource-order",
        "http-endpoint",
        "query-endpoint",
        "empty-authority-endpoint",
        "fragment-endpoint",
        "raw-secret",
        "state-alias",
    ],
)
def test_watchdog_schema_rejects_hostile_config_mutations(mutation: str) -> None:
    value = _watchdog_config()
    if mutation == "extra":
        value["start_new_release"] = True
    elif mutation == "wrong-resource-order":
        value["resource_kinds"] = list(reversed(value["resource_kinds"]))  # type: ignore[arg-type]
    elif mutation == "http-endpoint":
        value["lifecycle_cas"]["endpoint"] = "http://cas.invalid"  # type: ignore[index]
    elif mutation == "query-endpoint":
        value["lifecycle_cas"]["endpoint"] = (  # type: ignore[index]
            "https://cas.invalid/v2?token=secret"
        )
    elif mutation == "empty-authority-endpoint":
        value["lifecycle_cas"]["endpoint"] = "https:///v2"  # type: ignore[index]
    elif mutation == "fragment-endpoint":
        value["lifecycle_cas"]["endpoint"] = (  # type: ignore[index]
            "https://cas.invalid/v2#alternate"
        )
    elif mutation == "raw-secret":
        value["resource_recovery"]["password"] = "secret"  # type: ignore[index]
    else:
        value["state"]["cache_path"] = "/tmp/watchdog"  # type: ignore[index]
    with pytest.raises(ValidationError):
        _validate(_schema("watchdog-v2.schema.json"), value)


def test_schema_credential_names_match_only_systemd_credential_mounts() -> None:
    controller = _read("systemd/propertyquarry-release-control-v2@.service")
    watchdog = _read("systemd/propertyquarry-release-watchdog-v2.service")
    controller_config = json.dumps(_controller_config(), sort_keys=True)
    watchdog_config = json.dumps(_watchdog_config(), sort_keys=True)
    for name in (
        "request-authority-client",
        "lifecycle-cas-client",
        "evidence-store-client",
        "resource-mediator-client",
    ):
        assert name in controller
        assert name in controller_config
    for name in ("watchdog-takeover-client", "resource-recovery-client"):
        assert name in watchdog
        assert name in watchdog_config
    combined = controller_config + watchdog_config
    for forbidden in ('"token"', '"password"', '"secret"', '"private_key"'):
        assert forbidden not in combined


def test_systemd_units_have_no_syntax_errors_with_staged_native_binaries(
    tmp_path: Path,
) -> None:
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is unavailable")
    staged = tmp_path / "units"
    staged.mkdir()
    paths: list[Path] = []
    for source in SYSTEMD.iterdir():
        text = source.read_text(encoding="utf-8")
        text = re.sub(
            r"ExecStart=/usr/libexec/propertyquarry-release-control/[^ ]+",
            "ExecStart=/usr/bin/true",
            text,
        )
        target = staged / source.name
        target.write_text(text, encoding="utf-8")
        paths.append(target)
    completed = subprocess.run(
        ["systemd-analyze", "verify", *(str(path) for path in sorted(paths))],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "name",
    [
        "propertyquarry-release-control-v2@.service",
        "propertyquarry-release-watchdog-v2.service",
    ],
)
def test_systemd_offline_security_exposure_stays_below_two(
    name: str,
) -> None:
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is unavailable")
    completed = subprocess.run(
        [
            "systemd-analyze",
            "security",
            "--offline=yes",
            "--no-pager",
            str(SYSTEMD / name),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    match = re.search(r"Overall exposure level .*: ([0-9.]+)", completed.stdout)
    assert match is not None
    assert float(match.group(1)) < 2.0
