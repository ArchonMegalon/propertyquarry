from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scripts import propertyquarry_release_security_gate as gate


RELEASE_SHA = "a" * 40
WEB_IMAGE = f"registry.example/propertyquarry-web@sha256:{'b' * 64}"
RENDER_IMAGE = f"registry.example/propertyquarry-render@sha256:{'c' * 64}"
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def test_directory_identity_ignores_sibling_churn_but_binds_authority(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir(mode=0o700)
    before = trusted.stat()

    (trusted / "unrelated-sibling").mkdir(mode=0o700)
    after_sibling = trusted.stat()

    assert gate._directory_identity(after_sibling) == gate._directory_identity(
        before
    )

    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    assert gate._directory_identity(replacement.stat()) != gate._directory_identity(
        before
    )

    trusted.chmod(0o750)
    assert gate._directory_identity(trusted.stat()) != gate._directory_identity(
        before
    )


def _pip_payload(*, vulnerable: bool = False) -> list[dict[str, object]]:
    dependencies = [
        {"name": package, "version": version, "vulns": []}
        for package, version in gate.parse_requirements_lock().items()
    ]
    if vulnerable:
        fastapi = next(item for item in dependencies if item["name"] == "fastapi")
        fastapi["vulns"] = [
            {
                "id": "PYSEC-2026-001",
                "fix_versions": ["9.9.9"],
                "aliases": ["CVE-2026-1001"],
            }
        ]
    return dependencies


def _sbom_payload(target: str, *, components: bool = True) -> dict[str, object]:
    name, digest = target.rsplit("@", 1)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "container",
                "name": name,
                "version": digest,
            }
        },
        "components": (
            [{"type": "library", "name": "openssl", "version": "3.0.0"}]
            if components
            else []
        ),
    }


def _trivy_payload(
    *,
    target: str = WEB_IMAGE,
    severity: str | None = None,
    vulnerability_id: str = "CVE-2026-2001",
    artifact_name: str | None = None,
    repo_digests: list[str] | None = None,
) -> dict[str, object]:
    vulnerabilities: list[dict[str, str]] = []
    if severity:
        vulnerabilities.append(
            {
                "VulnerabilityID": vulnerability_id,
                "PkgName": "openssl",
                "InstalledVersion": "3.0.0",
                "FixedVersion": "3.0.1",
                "Severity": severity,
            }
        )
    return {
        "SchemaVersion": 2,
        "ArtifactName": artifact_name if artifact_name is not None else target,
        "ArtifactType": "container_image",
        "Metadata": {
            "RepoDigests": list(repo_digests) if repo_digests is not None else [target]
        },
        "Results": [{"Target": target, "Vulnerabilities": vulnerabilities}],
    }


class FakeRunner:
    def __init__(
        self,
        *,
        available: set[str] | None = None,
        pip_payload: object | None = None,
        web_sbom: Mapping[str, object] | None = None,
        render_sbom: Mapping[str, object] | None = None,
        web_trivy: Mapping[str, object] | None = None,
        render_trivy: Mapping[str, object] | None = None,
        pip_scan_returncode: int = 0,
        failures: Mapping[str, gate.CommandResult] | None = None,
        raw_outputs: Mapping[str, str] | None = None,
    ) -> None:
        self.available_tools = available if available is not None else set(gate.REQUIRED_TOOLS)
        self.pip_payload = pip_payload if pip_payload is not None else _pip_payload()
        self.web_sbom = dict(
            _sbom_payload(WEB_IMAGE) if web_sbom is None else web_sbom
        )
        self.render_sbom = dict(
            _sbom_payload(RENDER_IMAGE) if render_sbom is None else render_sbom
        )
        self.web_trivy = dict(
            _trivy_payload(target=WEB_IMAGE) if web_trivy is None else web_trivy
        )
        self.render_trivy = dict(
            _trivy_payload(target=RENDER_IMAGE) if render_trivy is None else render_trivy
        )
        self.pip_scan_returncode = pip_scan_returncode
        self.failures = dict(failures or {})
        self.raw_outputs = dict(raw_outputs or {})
        self.calls: list[tuple[str, ...]] = []

    def available(self, executable: str) -> bool:
        return executable in self.available_tools

    def identity(
        self,
        executable: str,
        *,
        now: datetime,
    ) -> Mapping[str, object]:
        versions = {
            "pip-audit": "2.9.0",
            "syft": "1.30.0",
            "trivy": "0.60.0",
        }
        return {
            "kind": "deterministic_test_scanner",
            "version": versions[executable],
            "execution": [f"/test-bin/{executable}"],
            "authenticated_at": gate.isoformat(now),
        }

    def run(self, argv: Sequence[str], *, timeout_seconds: int) -> gate.CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        executable = command[0]
        failure_key = executable
        if executable == "syft" and command[1].startswith("docker:"):
            failure_key = "syft-scan"
        elif executable == "trivy" and command[1] == "image":
            failure_key = "trivy-scan"
        elif executable == "pip-audit" and "--requirement" in command:
            failure_key = "pip-scan"
        if failure_key in self.failures:
            return self.failures[failure_key]
        if executable == "pip-audit" and "--version" in command:
            return gate.CommandResult(0, stdout="pip-audit 2.9.0")
        if executable == "syft" and command[1] == "--version":
            return gate.CommandResult(0, stdout="syft 1.30.0")
        if executable == "trivy" and command[1] == "--version":
            return gate.CommandResult(0, stdout="Version: 0.60.0")
        if executable == "pip-audit":
            if "pip" in self.raw_outputs:
                return gate.CommandResult(
                    self.pip_scan_returncode,
                    stdout=self.raw_outputs["pip"],
                )
            return gate.CommandResult(
                self.pip_scan_returncode, stdout=json.dumps(self.pip_payload)
            )
        if executable == "syft":
            is_render = "propertyquarry-render" in command[1]
            raw_key = "render-sbom" if is_render else "web-sbom"
            if raw_key in self.raw_outputs:
                return gate.CommandResult(0, stdout=self.raw_outputs[raw_key])
            payload = self.render_sbom if is_render else self.web_sbom
            return gate.CommandResult(0, stdout=json.dumps(payload))
        if executable == "trivy":
            is_render = "propertyquarry-render" in command[-1]
            raw_key = "render-trivy" if is_render else "web-trivy"
            if raw_key in self.raw_outputs:
                return gate.CommandResult(0, stdout=self.raw_outputs[raw_key])
            payload = self.render_trivy if is_render else self.web_trivy
            return gate.CommandResult(0, stdout=json.dumps(payload))
        raise AssertionError(f"unexpected command: {command}")


def _runtime_scanner_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    now: datetime,
    stale: bool = False,
) -> tuple[gate.SubprocessScannerRunner, dict[str, Path]]:
    runtime_root = tmp_path / "runtime"
    bin_dir = runtime_root / "bin"
    cache_dir = runtime_root / "trivy"
    db_dir = cache_dir / "db"
    java_db_dir = cache_dir / "java-db"
    bin_dir.mkdir(parents=True)
    db_dir.mkdir(parents=True)
    java_db_dir.mkdir(parents=True)

    paths = {
        "syft": bin_dir / "syft",
        "trivy": bin_dir / "trivy",
        "syft_config": runtime_root / "syft.v1.yaml",
        "trivy_config": runtime_root / "trivy.v1.yaml",
        "vulnerability_database": db_dir / "trivy.db",
        "vulnerability_metadata": db_dir / "metadata.json",
        "java_database": java_db_dir / "trivy-java.db",
        "java_metadata": java_db_dir / "metadata.json",
        "manifest": runtime_root / "security-runtime.v1.json",
    }
    paths["syft"].write_bytes(b"test-syft-binary\n")
    paths["trivy"].write_bytes(b"test-trivy-binary\n")
    paths["syft"].chmod(0o755)
    paths["trivy"].chmod(0o755)
    paths["syft_config"].write_bytes(b"{}\n")
    paths["trivy_config"].write_bytes(b"{}\n")
    paths["syft_config"].chmod(0o644)
    paths["trivy_config"].chmod(0o644)
    paths["vulnerability_database"].write_bytes(b"test-vulnerability-db\n")
    paths["java_database"].write_bytes(b"test-java-db\n")

    updated_at = now - timedelta(hours=2)
    downloaded_at = now - timedelta(hours=1)
    next_update = now if stale else now + timedelta(hours=12)
    metadata_documents = {
        "vulnerability": {
            "Version": 2,
            "NextUpdate": gate.isoformat(next_update),
            "UpdatedAt": gate.isoformat(updated_at),
            "DownloadedAt": gate.isoformat(downloaded_at),
        },
        "java": {
            "Version": 1,
            "NextUpdate": gate.isoformat(next_update),
            "UpdatedAt": gate.isoformat(updated_at),
            "DownloadedAt": gate.isoformat(downloaded_at),
        },
    }
    paths["vulnerability_metadata"].write_text(
        json.dumps(metadata_documents["vulnerability"]),
        encoding="utf-8",
    )
    paths["java_metadata"].write_text(
        json.dumps(metadata_documents["java"]),
        encoding="utf-8",
    )
    for database_path in (
        paths["vulnerability_database"],
        paths["vulnerability_metadata"],
        paths["java_database"],
        paths["java_metadata"],
    ):
        database_path.chmod(0o644)

    scanner_pins: Mapping[str, Mapping[str, object]] = {
        "syft": {
            "path": paths["syft"],
            "version": "1.44.0",
            "sha256": hashlib.sha256(paths["syft"].read_bytes()).hexdigest(),
            "config_path": paths["syft_config"],
            "config_sha256": hashlib.sha256(
                paths["syft_config"].read_bytes()
            ).hexdigest(),
        },
        "trivy": {
            "path": paths["trivy"],
            "version": "0.72.0",
            "sha256": hashlib.sha256(paths["trivy"].read_bytes()).hexdigest(),
            "config_path": paths["trivy_config"],
            "config_sha256": hashlib.sha256(
                paths["trivy_config"].read_bytes()
            ).hexdigest(),
        },
    }
    database_pins: Mapping[str, Mapping[str, object]] = {
        "vulnerability": {
            "database_path": paths["vulnerability_database"],
            "metadata_path": paths["vulnerability_metadata"],
            "schema_version": 2,
        },
        "java": {
            "database_path": paths["java_database"],
            "metadata_path": paths["java_metadata"],
            "schema_version": 1,
        },
    }
    manifest = {
        "schema": gate.SECURITY_RUNTIME_SCHEMA,
        "tools": {
            name: {
                "path": str(record["path"]),
                "version": record["version"],
                "sha256": record["sha256"],
                "config_path": str(record["config_path"]),
                "config_sha256": record["config_sha256"],
            }
            for name, record in scanner_pins.items()
        },
        "trivy_databases": {
            "cache_dir": str(cache_dir),
            **{
                name: {
                    "database_path": str(record["database_path"]),
                    "database_sha256": hashlib.sha256(
                        Path(record["database_path"]).read_bytes()
                    ).hexdigest(),
                    "metadata_path": str(record["metadata_path"]),
                    "metadata_sha256": hashlib.sha256(
                        Path(record["metadata_path"]).read_bytes()
                    ).hexdigest(),
                    "schema_version": record["schema_version"],
                    "updated_at": metadata_documents[name]["UpdatedAt"],
                    "downloaded_at": metadata_documents[name]["DownloadedAt"],
                    "next_update": metadata_documents[name]["NextUpdate"],
                }
                for name, record in database_pins.items()
            },
        },
    }
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    paths["manifest"].chmod(0o644)

    monkeypatch.setattr(gate, "PINNED_SCANNERS", scanner_pins)
    monkeypatch.setattr(gate, "TRIVY_DATABASES", database_pins)
    monkeypatch.setattr(gate, "TRIVY_CACHE_DIR", cache_dir)
    monkeypatch.setattr(gate, "SECURITY_RUNTIME_MANIFEST", paths["manifest"])
    owners = frozenset({0, os.geteuid()})
    return (
        gate.SubprocessScannerRunner(
            system_owners=owners,
            release_owners=owners,
        ),
        paths,
    )


def test_subprocess_runner_attests_pinned_binary_and_fresh_databases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, paths = _runtime_scanner_runner(
        tmp_path,
        monkeypatch,
        now=NOW,
    )

    identity = runner.identity("trivy", now=NOW)

    assert identity["kind"] == "root_owned_sha256_pinned_binary"
    assert identity["version"] == "0.72.0"
    assert identity["configuration"]["path"] == str(paths["trivy_config"])
    assert set(identity["databases"]) == {"vulnerability", "java"}
    assert identity["databases"]["vulnerability"]["next_update"] == gate.isoformat(
        NOW + timedelta(hours=12)
    )
    translated = runner._translated_command(
        ("trivy", "image", "--offline-scan", WEB_IMAGE)
    )
    private_cache = Path(translated[2])
    assert translated == [
        str(gate.PINNED_SCANNERS["trivy"]["path"]),
        "--cache-dir",
        str(private_cache),
        "--config",
        str(paths["trivy_config"]),
        "image",
        "--offline-scan",
        WEB_IMAGE,
    ]
    assert private_cache.name == "trivy-cache"
    assert os.readlink(private_cache / "db") == str(gate.TRIVY_CACHE_DIR / "db")
    assert os.readlink(private_cache / "java-db") == str(
        gate.TRIVY_CACHE_DIR / "java-db"
    )
    cache_root = private_cache.parent
    runner.close()
    assert not cache_root.exists()


def test_subprocess_runner_rejects_stale_trivy_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = _runtime_scanner_runner(
        tmp_path,
        monkeypatch,
        now=NOW,
        stale=True,
    )

    with pytest.raises(
        gate.SecurityValidationError,
        match="database is stale or has inconsistent timestamps",
    ):
        runner.identity("trivy", now=NOW)


def test_subprocess_runner_rejects_database_changed_after_root_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, paths = _runtime_scanner_runner(
        tmp_path,
        monkeypatch,
        now=NOW,
    )
    paths["vulnerability_database"].write_bytes(b"tampered-db\n")

    with pytest.raises(
        gate.SecurityValidationError,
        match="digest differs from its pin",
    ):
        runner.identity("trivy", now=NOW)


def test_subprocess_runner_rejects_scanner_configuration_changed_after_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, paths = _runtime_scanner_runner(
        tmp_path,
        monkeypatch,
        now=NOW,
    )
    paths["syft_config"].write_bytes(b"tampered: true\n")

    with pytest.raises(
        gate.SecurityValidationError,
        match="digest differs from its pin",
    ):
        runner.identity("syft", now=NOW)


def test_subprocess_runner_rejects_manifest_configuration_pin_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, paths = _runtime_scanner_runner(
        tmp_path,
        monkeypatch,
        now=NOW,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["tools"]["syft"]["config_sha256"] = "0" * 64
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        gate.SecurityValidationError,
        match="manifest syft pin is invalid",
    ):
        runner.identity("syft", now=NOW)


def test_security_atomic_writer_replaces_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text("keep-victim\n", encoding="utf-8")
    output = tmp_path / "receipt.json"
    output.symlink_to(victim.name)

    gate.atomic_write_json(output, {"status": "pass"}, overwrite=True)

    assert victim.read_text(encoding="utf-8") == "keep-victim\n"
    assert not output.is_symlink()
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "pass"
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_security_atomic_writer_no_overwrite_preserves_existing_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "receipt.json"
    output.write_text("keep-existing\n", encoding="utf-8")

    with pytest.raises(
        gate.SecurityValidationError,
        match="receipt already exists",
    ):
        gate.atomic_write_json(output, {"status": "pass"}, overwrite=False)

    assert output.read_text(encoding="utf-8") == "keep-existing\n"


def _write_waivers(path: Path, waivers: list[dict[str, object]] | None = None) -> Path:
    path.write_text(
        json.dumps({"schema": gate.WAIVER_SCHEMA, "waivers": waivers or []}),
        encoding="utf-8",
    )
    return path


def _config(
    tmp_path: Path,
    *,
    flagship: bool = True,
    threshold: str = "HIGH",
    waivers: list[dict[str, object]] | None = None,
    **overrides: object,
) -> gate.GateConfig:
    values: dict[str, object] = {
        "release_commit_sha": RELEASE_SHA,
        "web_image": WEB_IMAGE,
        "render_image": RENDER_IMAGE,
        "severity_threshold": threshold,
        "flagship": flagship,
        "waivers_path": _write_waivers(tmp_path / "waivers.json", waivers),
        "artifacts_dir": tmp_path / "artifacts",
        "receipt_path": tmp_path / "receipt.json",
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return gate.GateConfig(**values)  # type: ignore[arg-type]


def _waiver(
    *,
    waiver_id: str,
    source: str,
    target: str,
    vulnerability_id: str,
    package: str,
    severity: str,
    created_at: datetime = NOW - timedelta(days=1),
    expires_at: datetime = NOW + timedelta(days=7),
) -> dict[str, object]:
    return {
        "id": waiver_id,
        "source": source,
        "target": target,
        "vulnerability_id": vulnerability_id,
        "package": package,
        "severity": severity,
        "release_commit_sha": RELEASE_SHA,
        "owner": "security-owner",
        "approved_by": "release-approver",
        "reason": "Temporary mitigation is deployed and monitored.",
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def test_full_release_and_image_identities_are_required_before_scanning(tmp_path: Path) -> None:
    runner = FakeRunner()
    config = _config(tmp_path, web_image="propertyquarry-web:latest")
    receipt, exit_code = gate.run_security_gate(config=config, runner=runner, now=NOW)

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert "immutable image reference" in receipt["error"]["message"]
    assert runner.calls == []
    assert stat.S_IMODE(config.receipt_path.stat().st_mode) == 0o600


def test_missing_scanners_fail_closed_for_flagship(tmp_path: Path) -> None:
    runner = FakeRunner(available={"pip-audit"})
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path), runner=runner, now=NOW
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert receipt["gate_passed"] is False
    assert "syft" in receipt["error"]["message"]
    assert "trivy" in receipt["error"]["message"]
    assert runner.calls == []


def test_missing_scanners_are_advisory_for_ordinary_local_use(tmp_path: Path) -> None:
    runner = FakeRunner(available=set())
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path, flagship=False), runner=runner, now=NOW
    )

    assert exit_code == 0
    assert receipt["status"] == "advisory_unavailable"
    assert receipt["gate_passed"] is False
    assert runner.calls == []


def test_clean_flagship_scan_writes_sboms_scans_and_private_receipt(tmp_path: Path) -> None:
    runner = FakeRunner()
    config = _config(tmp_path)
    receipt, exit_code = gate.run_security_gate(config=config, runner=runner, now=NOW)

    assert exit_code == 0
    assert receipt["status"] == "pass"
    assert receipt["gate_passed"] is True
    assert receipt["summary"]["blocking"] == 0
    assert receipt["artifacts"]["web"]["component_count"] == 1
    assert receipt["artifacts"]["render"]["component_count"] == 1
    assert receipt["tools"]["pip-audit"]["identity"]["kind"] == (
        "deterministic_test_scanner"
    )
    assert receipt["tools"]["trivy"]["identity"]["version"] == "0.60.0"
    assert (config.artifacts_dir / "web.sbom.cdx.json").is_file()
    assert (config.artifacts_dir / "render.sbom.cdx.json").is_file()
    assert (config.artifacts_dir / "dependencies.pip-audit.json").is_file()
    assert stat.S_IMODE(config.receipt_path.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in config.artifacts_dir.iterdir())

    command_text = "\n".join(" ".join(call) for call in runner.calls)
    assert "docker:" + WEB_IMAGE in command_text
    assert "docker:" + RENDER_IMAGE in command_text
    assert "--skip-db-update" in command_text
    assert "--skip-java-db-update" in command_text
    assert "--skip-vex-repo-update" in command_text
    assert "--skip-version-check" in command_text
    assert "--offline-scan" in command_text
    assert "--cache-backend memory" in command_text
    assert "--scanners vuln" in command_text
    assert "trivy image --image-src docker" in command_text
    assert "--disable-pip" in command_text
    assert "--vulnerability-service osv" in command_text
    assert "docker pull" not in command_text
    assert "trivy sbom" not in command_text
    assert "syft registry:" not in command_text
    assert "pip install" not in command_text
    pip_call = next(
        call
        for call in runner.calls
        if call[0] == "pip-audit" and "--requirement" in call
    )
    snapshot_path = Path(pip_call[pip_call.index("--requirement") + 1])
    assert snapshot_path != gate.LOCK_PATH
    assert snapshot_path.name == "requirements.lock"
    assert snapshot_path.parent.name.startswith(
        "propertyquarry-security-input."
    )
    assert not snapshot_path.parent.exists()


def test_second_run_without_overwrite_preserves_entire_evidence_bundle(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    receipt, exit_code = gate.run_security_gate(
        config=config,
        runner=FakeRunner(),
        now=NOW,
    )
    assert exit_code == 0
    assert receipt["status"] == "pass"
    paths = (
        config.receipt_path,
        *gate.expected_artifact_paths(config),
    )
    before = {path: path.read_bytes() for path in paths}
    second_runner = FakeRunner()

    with pytest.raises(
        gate.SecurityValidationError,
        match="security output already exists",
    ):
        gate.run_security_gate(
            config=config,
            runner=second_runner,
            now=NOW,
        )

    assert second_runner.calls == []
    assert {path: path.read_bytes() for path in paths} == before


def test_gate_audits_private_lock_snapshot_and_rejects_source_lock_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_lock = gate.LOCK_PATH
    mutable_lock = tmp_path / "requirements.lock"
    mutable_lock.write_bytes(original_lock.read_bytes())
    mutable_lock.chmod(0o664)
    monkeypatch.setattr(gate, "LOCK_PATH", mutable_lock)

    class MutatingRunner(FakeRunner):
        snapshot_path: Path | None = None

        def run(
            self,
            argv: Sequence[str],
            *,
            timeout_seconds: int,
        ) -> gate.CommandResult:
            command = tuple(argv)
            if command[0] == "pip-audit" and "--requirement" in command:
                self.snapshot_path = Path(
                    command[command.index("--requirement") + 1]
                )
                assert self.snapshot_path != mutable_lock
                assert self.snapshot_path.read_bytes() == mutable_lock.read_bytes()
                mutable_lock.write_bytes(
                    mutable_lock.read_bytes() + b"# concurrent drift\n"
                )
            return super().run(argv, timeout_seconds=timeout_seconds)

    runner = MutatingRunner()
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=runner,
        now=NOW,
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert "digest differs from its pin" in receipt["error"]["message"]
    assert runner.snapshot_path is not None
    assert not runner.snapshot_path.parent.exists()


def test_gate_rejects_authenticated_scanner_identity_drift(
    tmp_path: Path,
) -> None:
    class DriftingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.identity_counts: dict[str, int] = {}

        def identity(
            self,
            executable: str,
            *,
            now: datetime,
        ) -> Mapping[str, object]:
            count = self.identity_counts.get(executable, 0) + 1
            self.identity_counts[executable] = count
            identity = dict(super().identity(executable, now=now))
            if executable == "trivy" and count > 1:
                identity["runtime_epoch"] = "changed"
            return identity

    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=DriftingRunner(),
        now=NOW,
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert (
        "authenticated scanner runtime changed during the security scan"
        in receipt["error"]["message"]
    )


@pytest.mark.parametrize(
    ("case", "detail"),
    [
        ("empty", "missing:"),
        ("partial", "missing:"),
        ("wrong-version", "version mismatch:"),
        ("unexpected", "unexpected:"),
    ],
)
def test_pip_audit_must_exactly_cover_selected_lock(
    tmp_path: Path,
    case: str,
    detail: str,
) -> None:
    payload = _pip_payload()
    if case == "empty":
        payload = []
    elif case == "partial":
        payload.pop()
    elif case == "wrong-version":
        payload[0]["version"] = "0"
    elif case == "unexpected":
        payload.append({"name": "not-in-the-lock", "version": "1", "vulns": []})

    config = _config(tmp_path)
    receipt, exit_code = gate.run_security_gate(
        config=config,
        runner=FakeRunner(pip_payload=payload),
        now=NOW,
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert "does not exactly cover the selected requirements lock" in receipt["error"]["message"]
    assert detail in receipt["error"]["message"]
    assert not (config.artifacts_dir / "dependencies.pip-audit.json").exists()


def test_pip_audit_coverage_normalizes_package_names(tmp_path: Path) -> None:
    payload = _pip_payload()
    dependency = next(item for item in payload if item["name"] == "charset-normalizer")
    dependency["name"] = "Charset_Normalizer"

    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=FakeRunner(pip_payload=payload),
        now=NOW,
    )

    assert exit_code == 0
    assert receipt["status"] == "pass"


def test_pip_audit_rejects_duplicate_normalized_package_entries(tmp_path: Path) -> None:
    payload = _pip_payload()
    dependency = next(item for item in payload if item["name"] == "charset-normalizer")
    payload.append(
        {
            "name": "Charset_Normalizer",
            "version": dependency["version"],
            "vulns": [],
        }
    )

    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=FakeRunner(pip_payload=payload),
        now=NOW,
    )

    assert exit_code == 2
    assert "duplicate normalized package 'charset-normalizer'" in receipt["error"]["message"]


def test_trivy_accepts_current_artifact_name_and_repo_digest_identity_forms(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        web_trivy=_trivy_payload(
            target=WEB_IMAGE,
            repo_digests=[],
        ),
        render_trivy=_trivy_payload(
            target=RENDER_IMAGE,
            artifact_name="propertyquarry-render:local",
            repo_digests=[RENDER_IMAGE],
        ),
    )

    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=runner,
        now=NOW,
    )

    assert exit_code == 0
    assert receipt["status"] == "pass"


def test_sbom_must_prove_corresponding_immutable_image_target(tmp_path: Path) -> None:
    runner = FakeRunner(web_sbom=_sbom_payload(RENDER_IMAGE))
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=runner,
        now=NOW,
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert "web SBOM does not prove the expected immutable image target" in receipt["error"]["message"]


def test_trivy_report_must_prove_corresponding_immutable_image_target(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(web_trivy=_trivy_payload(target=RENDER_IMAGE))
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=runner,
        now=NOW,
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert "trivy:web output does not prove the expected immutable image target" in receipt[
        "error"
    ]["message"]


def test_empty_trivy_results_fail_flagship_closed(tmp_path: Path) -> None:
    trivy_payload = _trivy_payload(target=WEB_IMAGE)
    trivy_payload["Results"] = []
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=FakeRunner(web_trivy=trivy_payload),
        now=NOW,
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert "non-empty Results list" in receipt["error"]["message"]


@pytest.mark.parametrize(
    ("raw_key", "raw_output", "document_name"),
    [
        (
            "pip",
            json.dumps(_pip_payload()).replace(
                '"name": "annotated-doc"',
                '"name": "annotated-doc", "name": "annotated-doc"',
                1,
            ),
            "pip-audit output",
        ),
        (
            "web-sbom",
            json.dumps(_sbom_payload(WEB_IMAGE)).replace(
                '"bomFormat": "CycloneDX"',
                '"bomFormat": "CycloneDX", "bomFormat": "CycloneDX"',
                1,
            ),
            "web Syft SBOM",
        ),
        (
            "web-trivy",
            json.dumps(_trivy_payload(target=WEB_IMAGE)).replace(
                '"SchemaVersion": 2',
                '"SchemaVersion": 2, "SchemaVersion": 2',
                1,
            ),
            "web Trivy output",
        ),
    ],
)
def test_duplicate_json_keys_fail_closed_through_full_gate(
    tmp_path: Path,
    raw_key: str,
    raw_output: str,
    document_name: str,
) -> None:
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path),
        runner=FakeRunner(raw_outputs={raw_key: raw_output}),
        now=NOW,
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert document_name in receipt["error"]["message"]
    assert "duplicate object key" in receipt["error"]["message"]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_parser_rejects_non_finite_constants(constant: str) -> None:
    with pytest.raises(gate.SecurityValidationError, match="non-finite numeric constant"):
        gate.parse_json_document(
            f'{{"value": {constant}}}',
            document_name="adversarial document",
        )


def test_high_and_unknown_findings_block_flagship_at_high_threshold(tmp_path: Path) -> None:
    runner = FakeRunner(
        pip_payload=_pip_payload(vulnerable=True),
        pip_scan_returncode=1,
        web_trivy=_trivy_payload(severity="LOW"),
        render_trivy=_trivy_payload(
            target=RENDER_IMAGE,
            severity="HIGH",
            vulnerability_id="CVE-2026-3001",
        ),
    )
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path), runner=runner, now=NOW
    )

    assert exit_code == 1
    assert receipt["status"] == "failed"
    assert receipt["summary"] == {
        "total": 3,
        "at_or_above_threshold": 2,
        "waived": 0,
        "blocking": 2,
        "by_effective_severity": {
            "CRITICAL": 1,
            "HIGH": 1,
            "MEDIUM": 0,
            "LOW": 1,
        },
    }
    pip_finding = next(item for item in receipt["findings"] if item["source"] == "pip-audit")
    assert pip_finding["severity"] == "UNKNOWN"
    assert pip_finding["effective_severity"] == "CRITICAL"


def test_vulnerabilities_remain_advisory_outside_flagship_mode(tmp_path: Path) -> None:
    runner = FakeRunner(
        render_trivy=_trivy_payload(target=RENDER_IMAGE, severity="CRITICAL")
    )
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path, flagship=False), runner=runner, now=NOW
    )

    assert exit_code == 0
    assert receipt["status"] == "advisory_findings"
    assert receipt["gate_passed"] is False
    assert receipt["summary"]["blocking"] == 1


def test_exact_release_bound_time_limited_waivers_clear_findings(tmp_path: Path) -> None:
    dependency_hash = hashlib.sha256(gate.LOCK_PATH.read_bytes()).hexdigest()
    waivers = [
        _waiver(
            waiver_id="PQSEC-2026-001",
            source="pip-audit",
            target=f"sha256:{dependency_hash}",
            vulnerability_id="PYSEC-2026-001",
            package="fastapi",
            severity="UNKNOWN",
        ),
        _waiver(
            waiver_id="PQSEC-2026-002",
            source="trivy:render",
            target=RENDER_IMAGE,
            vulnerability_id="CVE-2026-3001",
            package="openssl",
            severity="HIGH",
        ),
    ]
    runner = FakeRunner(
        pip_payload=_pip_payload(vulnerable=True),
        render_trivy=_trivy_payload(
            target=RENDER_IMAGE,
            severity="HIGH",
            vulnerability_id="CVE-2026-3001",
        ),
    )
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path, waivers=waivers), runner=runner, now=NOW
    )

    assert exit_code == 0
    assert receipt["status"] == "pass"
    assert receipt["summary"]["at_or_above_threshold"] == 2
    assert receipt["summary"]["waived"] == 2
    assert receipt["summary"]["blocking"] == 0
    assert [item["id"] for item in receipt["waivers"]["applied"]] == [
        "PQSEC-2026-001",
        "PQSEC-2026-002",
    ]


@pytest.mark.parametrize(
    ("created_at", "expires_at", "message"),
    [
        (NOW - timedelta(days=10), NOW - timedelta(seconds=1), "expired"),
        (NOW - timedelta(days=1), NOW + timedelta(days=31), "within 30 days"),
        (NOW + timedelta(seconds=1), NOW + timedelta(days=1), "cannot be in the future"),
    ],
)
def test_expired_future_or_overlong_waivers_fail_before_scanning(
    tmp_path: Path,
    created_at: datetime,
    expires_at: datetime,
    message: str,
) -> None:
    dependency_hash = hashlib.sha256(gate.LOCK_PATH.read_bytes()).hexdigest()
    waiver = _waiver(
        waiver_id="PQSEC-2026-099",
        source="pip-audit",
        target=f"sha256:{dependency_hash}",
        vulnerability_id="PYSEC-2026-001",
        package="fastapi",
        severity="UNKNOWN",
        created_at=created_at,
        expires_at=expires_at,
    )
    runner = FakeRunner()
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path, waivers=[waiver]), runner=runner, now=NOW
    )

    assert exit_code == 2
    assert message in receipt["error"]["message"]
    assert runner.calls == []


def test_waiver_requires_independent_approver(tmp_path: Path) -> None:
    dependency_hash = hashlib.sha256(gate.LOCK_PATH.read_bytes()).hexdigest()
    waiver = _waiver(
        waiver_id="PQSEC-2026-100",
        source="pip-audit",
        target=f"sha256:{dependency_hash}",
        vulnerability_id="PYSEC-2026-001",
        package="fastapi",
        severity="UNKNOWN",
    )
    waiver["approved_by"] = waiver["owner"]
    runner = FakeRunner()
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path, waivers=[waiver]), runner=runner, now=NOW
    )

    assert exit_code == 2
    assert "independent from the waiver owner" in receipt["error"]["message"]
    assert runner.calls == []


def test_missing_or_empty_sbom_fails_flagship_closed(tmp_path: Path) -> None:
    runner = FakeRunner(web_sbom=_sbom_payload(WEB_IMAGE, components=False))
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path), runner=runner, now=NOW
    )

    assert exit_code == 2
    assert receipt["status"] == "failed"
    assert "at least one component" in receipt["error"]["message"]
    assert receipt["artifacts"].get("web") is None


def test_scanner_failure_with_secret_output_is_withheld_from_receipt(tmp_path: Path) -> None:
    secret = "registry-token-super-secret"
    runner = FakeRunner(
        failures={
            "trivy-scan": gate.CommandResult(
                7, stdout=secret, stderr=f"authorization={secret}"
            )
        }
    )
    receipt, exit_code = gate.run_security_gate(
        config=_config(tmp_path), runner=runner, now=NOW
    )

    assert exit_code == 2
    serialized = json.dumps(receipt)
    assert secret not in serialized
    assert "raw output was withheld" in receipt["error"]["message"]


def test_subprocess_runner_uses_private_cache_and_sanitized_scanner_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_environments: list[dict[str, str]] = []
    observed_commands: list[tuple[str, ...]] = []

    class Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(*args: object, **kwargs: object) -> Completed:
        observed_environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        command = tuple(args[0])  # type: ignore[arg-type]
        observed_commands.append(command)
        if command[0] == "/trusted/trivy":
            cache = Path(command[command.index("--cache-dir") + 1])
            fanal = cache / "fanal"
            fanal.mkdir(mode=0o700)
            database = fanal / "fanal.db"
            database.write_bytes(b"private cache")
            database.chmod(0o600)
        return Completed()

    authenticated_cache = tmp_path / "authenticated-trivy-cache"
    (authenticated_cache / "db").mkdir(mode=0o755, parents=True)
    (authenticated_cache / "java-db").mkdir(mode=0o755)
    monkeypatch.setattr(gate, "TRIVY_CACHE_DIR", authenticated_cache)
    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    runner = gate.SubprocessScannerRunner(
        system_owners=frozenset({os.geteuid()})
    )
    monkeypatch.setattr(
        runner,
        "_pip_audit_identity",
        lambda: {
            "execution": [
                "/trusted/release-python",
                "-I",
                "-B",
                "-m",
                "pip_audit",
            ]
        },
    )
    monkeypatch.setattr(
        runner,
        "_binary_identity",
        lambda executable: {
            "execution": [f"/trusted/{executable}"],
            "configuration": {
                "path": f"/trusted/{executable}.yaml",
            },
        },
    )
    runner.run(("pip-audit", "--version"), timeout_seconds=10)
    runner.run(("syft", "--version"), timeout_seconds=10)
    runner.run(("trivy", "--version"), timeout_seconds=10)

    cache_option = observed_commands[0].index("--cache-dir")
    cache_path = Path(observed_commands[0][cache_option + 1])
    cache_root = cache_path.parent
    assert cache_path.is_dir()
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o700
    assert observed_commands[0][:5] == (
        "/trusted/release-python",
        "-I",
        "-B",
        "-m",
        "pip_audit",
    )
    assert observed_commands[1][:3] == (
        "/trusted/syft",
        "--config",
        "/trusted/syft.yaml",
    )
    assert observed_commands[2][:5] == (
        "/trusted/trivy",
        "--cache-dir",
        observed_commands[2][2],
        "--config",
        "/trusted/trivy.yaml",
    )
    trivy_cache = Path(observed_commands[2][2])
    assert trivy_cache.parent == cache_root
    assert trivy_cache.name == "trivy-cache"
    assert stat.S_IMODE(trivy_cache.stat().st_mode) == 0o700
    assert (trivy_cache / "db").is_symlink()
    assert os.readlink(trivy_cache / "db") == str(authenticated_cache / "db")
    assert (trivy_cache / "java-db").is_symlink()
    assert os.readlink(trivy_cache / "java-db") == str(
        authenticated_cache / "java-db"
    )
    assert stat.S_IMODE((trivy_cache / "fanal" / "fanal.db").stat().st_mode) == 0o600
    assert observed_environments[1]["SYFT_CHECK_FOR_APP_UPDATE"] == "false"
    assert observed_environments[2]["TRIVY_SKIP_VERSION_CHECK"] == "true"
    assert all(environment["HOME"] == "/nonexistent" for environment in observed_environments)
    assert all("HTTPS_PROXY" not in environment for environment in observed_environments)

    runner.close()

    assert not cache_root.exists()


def test_private_trivy_cache_rejects_database_link_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticated_cache = tmp_path / "authenticated-trivy-cache"
    (authenticated_cache / "db").mkdir(mode=0o755, parents=True)
    (authenticated_cache / "java-db").mkdir(mode=0o755)
    substituted = tmp_path / "substituted-db"
    substituted.mkdir(mode=0o700)
    monkeypatch.setattr(gate, "TRIVY_CACHE_DIR", authenticated_cache)
    runner = gate.SubprocessScannerRunner(
        system_owners=frozenset({os.geteuid()})
    )
    monkeypatch.setattr(
        runner,
        "_binary_identity",
        lambda _executable: {
            "execution": ["/trusted/trivy"],
            "configuration": {"path": "/trusted/trivy.yaml"},
        },
    )
    cache = runner._trivy_cache_dir()
    (cache / "db").unlink()
    (cache / "db").symlink_to(substituted, target_is_directory=True)
    try:
        with pytest.raises(
            gate.ScannerExecutionError,
            match="private Trivy runtime cache was mutated",
        ):
            runner.run(("trivy", "--version"), timeout_seconds=10)
    finally:
        runner.close()


def test_main_reports_owned_scanner_cleanup_failure_without_pass_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class CleanupFailingRunner(FakeRunner):
        def close(self) -> None:
            raise gate.ScannerExecutionError("simulated cleanup failure")

    monkeypatch.setattr(gate, "SubprocessScannerRunner", CleanupFailingRunner)
    artifacts = tmp_path / "artifacts"
    receipt_path = tmp_path / "receipt.json"

    exit_code = gate.main(
        [
            "--flagship",
            "--release-sha",
            RELEASE_SHA,
            "--web-image",
            WEB_IMAGE,
            "--render-image",
            RENDER_IMAGE,
            "--severity-threshold",
            "HIGH",
            "--artifacts-dir",
            str(artifacts),
            "--receipt",
            str(receipt_path),
        ]
    )

    captured = capsys.readouterr()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert "Traceback" not in captured.err
    assert receipt["status"] == "failed"
    assert receipt["gate_passed"] is False
    assert receipt["error"] == {
        "type": "ScannerCleanupError",
        "message": "private scanner cleanup failed",
    }


def _workflow_job(workflow: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    start = workflow.index(marker)
    body_start = start + len(marker)
    next_job = re.search(r"^  [a-zA-Z0-9_-]+:\n", workflow[body_start:], flags=re.MULTILINE)
    end = body_start + next_job.start() if next_job else len(workflow)
    return workflow[start:end]


def test_legacy_flagship_security_job_is_disabled_and_not_release_authority() -> None:
    workflow = (gate.APP_ROOT / ".github/workflows/smoke-runtime.yml").read_text(
        encoding="utf-8"
    )
    job = _workflow_job(workflow, "propertyquarry-flagship-security")
    release_job = _workflow_job(workflow, "propertyquarry-release-v2")

    assert "if: ${{ false }}" in job
    assert "environment:\n      name: propertyquarry-production" in job
    assert "permissions:\n      contents: read" in job
    assert "runs-on: [self-hosted, propertyquarry-security]" in job
    assert "persist-credentials: false" in job
    assert "command -v pip-audit" not in job
    assert "command -v syft" not in job
    assert "command -v trivy" not in job
    assert "PROPERTYQUARRY_WEB_IMAGE: ${{ vars.PROPERTYQUARRY_WEB_IMAGE }}" in job
    assert "PROPERTYQUARRY_RENDER_IMAGE: ${{ vars.PROPERTYQUARRY_RENDER_IMAGE }}" in job
    assert "--severity-threshold HIGH" in job
    assert "--flagship" in job
    assert "propertyquarry_security_waivers.json" in job
    assert (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4"
        in job
    )
    assert "pip install" not in job
    assert "apt-get" not in job
    assert "docker pull" not in job
    assert "docker compose" not in job
    assert "ea-api" not in job
    assert "propertyquarry-flagship-security" not in release_job
    assert "secrets." not in release_job
    assert "vars." not in release_job
    assert "actions/checkout" not in release_job
    assert "PROPERTYQUARRY_WORKFLOW_HEAD_SHA: ${{ github.sha }}" in job
    assert "release_manifest_runtime_sha" in job
    assert "workflow-binding.json" in job
