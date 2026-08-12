from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import stat

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "provision_propertyquarry_auth_env.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "provision_propertyquarry_auth_env", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_source(path: Path, *, sender: str = "access@propertyquarry.com") -> None:
    path.write_text(
        "\n".join(
            (
                "EMAILIT_API_KEY=emailit-private-key",
                f"EA_EMAIL_DEFAULT_FROM={sender}",
                "EA_EMAIL_DEFAULT_NAME=PropertyQuarry",
                f"EA_REGISTRATION_EMAIL_FROM={sender}",
                "EA_REGISTRATION_EMAIL_NAME=PropertyQuarry",
                "EA_GOOGLE_OAUTH_CLIENT_ID=google-client-id",
                "EA_GOOGLE_OAUTH_CLIENT_SECRET=google-client-secret",
                "EA_GOOGLE_OAUTH_STATE_SECRET=shared-state-secret-that-must-not-be-copied",
                "EA_PROVIDER_SECRET_KEY=shared-provider-secret-that-must-not-be-copied",
                "EA_EDGE_PRINCIPAL_ASSERTION_SECRET=shared-edge-secret-that-must-not-be-copied",
                "PROPERTYQUARRY_RELEASE_PROBE_SECRET=shared-release-probe-secret-that-must-not-be-copied",
                "UNRELATED_ROOT_TOKEN=must-not-cross-boundary",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_provisioner_writes_narrow_mode_0600_environment(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "source.env"
    output = tmp_path / "runtime" / "propertyquarry_auth.env"
    receipt_path = tmp_path / "runtime" / "propertyquarry_auth_receipt.json"
    _write_source(source)

    receipt = module.provision_auth_environment(
        source_env=source,
        output_env=output,
        receipt_path=receipt_path,
    )

    values = module.parse_env_file(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert (
        values["EA_GOOGLE_OAUTH_REDIRECT_URI"]
        == "https://propertyquarry.com/google/callback"
    )
    assert values["EMAILIT_API_KEY"] == "emailit-private-key"
    assert values["EA_GOOGLE_OAUTH_CLIENT_SECRET"] == "google-client-secret"
    assert (
        values["EA_GOOGLE_OAUTH_STATE_SECRET"]
        != "shared-state-secret-that-must-not-be-copied"
    )
    assert (
        values["EA_PROVIDER_SECRET_KEY"]
        != "shared-provider-secret-that-must-not-be-copied"
    )
    assert (
        values["EA_EDGE_PRINCIPAL_ASSERTION_SECRET"]
        != "shared-edge-secret-that-must-not-be-copied"
    )
    assert (
        values["EA_EDGE_PRINCIPAL_ASSERTION_AUDIENCE"]
        == "propertyquarry-local-ops-v1"
    )
    assert (
        values["PROPERTYQUARRY_RELEASE_PROBE_SECRET"]
        != "shared-release-probe-secret-that-must-not-be-copied"
    )
    assert (
        values["PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID"]
        == "propertyquarry-release-probe"
    )
    assert (
        values["PROPERTYQUARRY_RELEASE_PROBE_ORIGIN"]
        == "https://propertyquarry.com"
    )
    assert (
        values["PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE"]
        == "/app/research/perf-candidate-1020?run_id=run-gold-mobile"
    )
    assert (
        values["PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH"]
        == "/app/shortlist/run/0a89ead9e0b048288cca22d1aac54fa7"
    )
    assert "UNRELATED_ROOT_TOKEN" not in values
    assert receipt["status"] == "ready"
    assert receipt["edge_assertion_configured"] is True
    assert receipt["edge_assertion_audience"] == "propertyquarry-local-ops-v1"
    assert receipt["dedicated_edge_assertion_secret"] is True
    assert receipt["release_probe_configured"] is True
    assert receipt["release_probe_route_values_redacted"] is True
    assert receipt["release_probe_research_detail_route_sha256"] == hashlib.sha256(
        values[
            "PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE"
        ].encode("utf-8")
    ).hexdigest()
    assert receipt["release_probe_shortlist_run_path_sha256"] == hashlib.sha256(
        values[
            "PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH"
        ].encode("utf-8")
    ).hexdigest()
    assert receipt["dedicated_release_probe_secret"] is True
    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert "emailit-private-key" not in receipt_text
    assert "google-client-secret" not in receipt_text
    assert values["EA_EDGE_PRINCIPAL_ASSERTION_SECRET"] not in receipt_text
    assert values["PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE"] not in receipt_text
    assert values["PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH"] not in receipt_text
    persisted_receipt = json.loads(receipt_text)
    assert "release_probe_research_detail_route" not in persisted_receipt
    assert "release_probe_shortlist_run_path" not in persisted_receipt
    assert persisted_receipt["unrelated_source_keys_copied"] is False


def test_provisioner_replay_preserves_dedicated_secrets(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "source.env"
    output = tmp_path / "propertyquarry_auth.env"
    receipt_path = tmp_path / "propertyquarry_auth_receipt.json"
    _write_source(source)

    module.provision_auth_environment(
        source_env=source, output_env=output, receipt_path=receipt_path
    )
    first = module.parse_env_file(output)
    module.provision_auth_environment(
        source_env=source, output_env=output, receipt_path=receipt_path
    )
    second = module.parse_env_file(output)

    assert (
        second["EA_GOOGLE_OAUTH_STATE_SECRET"] == first["EA_GOOGLE_OAUTH_STATE_SECRET"]
    )
    assert second["EA_PROVIDER_SECRET_KEY"] == first["EA_PROVIDER_SECRET_KEY"]
    assert (
        second["EA_EDGE_PRINCIPAL_ASSERTION_SECRET"]
        == first["EA_EDGE_PRINCIPAL_ASSERTION_SECRET"]
    )
    assert (
        second["PROPERTYQUARRY_RELEASE_PROBE_SECRET"]
        == first["PROPERTYQUARRY_RELEASE_PROBE_SECRET"]
    )


def test_provisioner_validates_release_probe_overrides(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "source.env"
    _write_source(source)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            "PROPERTYQUARRY_RELEASE_PROBE_ORIGIN=https://staging.propertyquarry.com/\n"
            "PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID=propertyquarry-staging-probe\n"
            "PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE=/app/research/staging-candidate?run_id=staging-run\n"
            "PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH=/app/shortlist/run/staging-run\n"
        )

    module.provision_auth_environment(
        source_env=source,
        output_env=tmp_path / "propertyquarry_auth.env",
        receipt_path=tmp_path / "propertyquarry_auth_receipt.json",
    )
    values = module.parse_env_file(tmp_path / "propertyquarry_auth.env")

    assert (
        values["PROPERTYQUARRY_RELEASE_PROBE_ORIGIN"]
        == "https://staging.propertyquarry.com"
    )
    assert (
        values["PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID"]
        == "propertyquarry-staging-probe"
    )


def test_provisioner_rejects_non_propertyquarry_sender(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "source.env"
    _write_source(source, sender="access@example.test")

    with pytest.raises(
        module.AuthEnvProvisionError, match="propertyquarry_sender_domain_required"
    ):
        module.provision_auth_environment(
            source_env=source,
            output_env=tmp_path / "output.env",
            receipt_path=tmp_path / "receipt.json",
        )


def test_provisioner_rejects_symlink_output(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "source.env"
    target = tmp_path / "target.env"
    output = tmp_path / "output.env"
    _write_source(source)
    target.write_text("sentinel=1\n", encoding="utf-8")
    output.symlink_to(target)

    with pytest.raises(
        module.AuthEnvProvisionError, match="output_path_symlink_rejected"
    ):
        module.provision_auth_environment(
            source_env=source,
            output_env=output,
            receipt_path=tmp_path / "receipt.json",
        )
    assert target.read_text(encoding="utf-8") == "sentinel=1\n"


def test_provisioner_rejects_receipt_overwriting_auth_environment(
    tmp_path: Path,
) -> None:
    module = _load_module()
    source = tmp_path / "source.env"
    output = tmp_path / "propertyquarry_auth.env"
    _write_source(source)

    with pytest.raises(
        module.AuthEnvProvisionError, match="auth_env_paths_must_be_distinct"
    ):
        module.provision_auth_environment(
            source_env=source,
            output_env=output,
            receipt_path=output,
        )
    assert not output.exists()
