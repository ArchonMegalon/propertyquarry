from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.product import property_tour_ai_panorama_admission as admission_authority
from scripts import propertyquarry_ai_panorama_controller_contract as contract
from scripts import propertyquarry_prater_ai_panorama_controller as controller


_REAL_DATABASE_SECRET_ENVIRONMENT = controller._database_secret_environment


@pytest.fixture(autouse=True)
def _private_db_secret_context(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.contextmanager
    def _loaded(_admission: object):  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr(controller, "_database_secret_environment", _loaded)


def _trusted() -> SimpleNamespace:
    return SimpleNamespace(
        subject=(
            "repo:ArchonMegalon@11421547/propertyquarry@1257593732:"
            "environment:propertyquarry-production"
        ),
        actor_principal_id="propertyquarry-ai-panorama-controller",
        repository="ArchonMegalon/propertyquarry",
        git_ref="refs/heads/main",
        git_head_sha="a" * 40,
        workflow_ref=(
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "smoke-runtime.yml@refs/heads/main"
        ),
        job="propertyquarry-release-v2",
        environment="propertyquarry-production",
        review_receipt_sha256="b" * 64,
        web_image=(
            "ghcr.io/archonmegalon/"
            "propertyquarry-standalone-web-runtime@sha256:" + "1" * 64
        ),
        web_image_id="sha256:" + "2" * 64,
        key_usage="propertyquarry.ai-panorama-install-permit.signing.v1",
        key_id="ai-panorama-production-1",
        key_epoch=1,
        key_sha256="c" * 64,
        keyring_sha256="3" * 64,
        volume_profile_sha256="d" * 64,
        compose_plan_sha256="e" * 64,
        volume_id="propertyquarry-governed-public-tours-production",
        artifact_root_device=41,
        artifact_root_inode=42,
        public_tour_root_device=51,
        public_tour_root_inode=52,
        execution_lease_seconds=300,
    )


def _release_module() -> SimpleNamespace:
    return SimpleNamespace(
        PRATER_SEARCH_RUN_ID="run",
        PRATER_CANDIDATE_REF="candidate",
        PRATER_EXTERNAL_ID="1807240910",
        PRATER_LISTING_URL="https://example.invalid/listing/1807240910",
        PRATER_SOURCE_REF="property-scout:1807240910",
        PRATER_PROVIDER_KEY="willhaben",
        PRATER_SLUG="prater-tour",
        PRATER_SOURCE_TREE_SHA256="1" * 64,
        PRATER_TOUR_SHA256="2" * 64,
        PRATER_CORE_MANIFEST_SHA256="3" * 64,
        PRATER_MATERIALIZATION_RECEIPT_SHA256="4" * 64,
        PRATER_CANDIDATE_MARKER_SHA256="5" * 64,
        PRATER_ARTIFACT_RELPATH="incoming/prater-tour",
        PRATER_MATERIALIZATION_RECEIPT_RELPATH="runtime/prater.json",
    )


def _request() -> dict[str, str]:
    return {
        "owner_principal_id": "private-owner@example.invalid",
        "expected_publication_record_sha256": "6" * 64,
        "request_id": "7" * 32,
    }


def test_source_contract_is_inert_and_hashes_exact_controller_sources() -> None:
    value = contract.build_info()

    assert value["authoritative"] is False
    assert value["production_ready"] is False
    assert value["performs_release_effects"] is False
    assert len(str(value["source_manifest_sha256"])) == 64
    relpaths = {row["relpath"] for row in value["files"]}
    assert "scripts/propertyquarry_prater_ai_panorama_controller.py" in relpaths
    assert "ea/app/product/property_tour_ai_panorama_admission.py" in relpaths
    assert "ea/app/product/property_tour_ai_panorama_operation_journal.py" in relpaths
    assert "ea/app/product/property_tour_governed_reservations.py" in relpaths
    assert "ea/app/product/service.py" in relpaths
    assert "ea/Dockerfile.property" in relpaths
    assert "scripts/property_tour_governed_reservation.py" in relpaths
    assert (
        "scripts/propertyquarry_prater_governed_volume_bootstrap.py"
        in relpaths
    )
    assert "scripts/propertyquarry_prater_ai_panorama_closeout.py" in relpaths
    assert "ea/Dockerfile.property-web" in relpaths
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "ea"
        / "Dockerfile.property-web"
    ).read_text(encoding="utf-8")
    assert (
        "/usr/local/libexec/"
        "propertyquarry-prater-ai-panorama-record-discovery-v1.py"
    ) in dockerfile
    assert (
        "/usr/local/libexec/"
        "propertyquarry-prater-governed-volume-bootstrap-v1.py"
    ) in dockerfile
    assert (
        "/usr/local/libexec/"
        "propertyquarry-prater-ai-panorama-closeout-v1.py"
    ) in dockerfile


def test_controller_state_genesis_bytes_are_exact_and_non_recreatable() -> None:
    values = contract.build_state_genesis(
        consumption_instance_id="a" * 32,
        operation_instance_id="b" * 32,
    )

    assert set(values) == {
        "consumption-ledger.v2.json",
        "consumption-ledger.v2.lock",
        "operation-journal.v1.json",
        "operation-journal.v1.lock",
    }
    assert values["consumption-ledger.v2.lock"] == b"lock\n"
    assert values["operation-journal.v1.lock"] == b"lock\n"
    for name in (
        "consumption-ledger.v2.json",
        "operation-journal.v1.json",
    ):
        decoded = json.loads(values[name].decode("ascii"))
        assert values[name] == contract._canonical(decoded) + b"\n"
        assert decoded["sequence"] == 0
        assert decoded["tip_sha256"] == "0" * 64
        assert decoded["entries"] == []
    with pytest.raises(
        contract.ControllerContractError,
        match="ai-panorama-controller-state-instance-invalid",
    ):
        contract.build_state_genesis(
            consumption_instance_id="a" * 32,
            operation_instance_id="a" * 32,
        )


def test_fixed_request_loader_rejects_noncanonical_or_extra_fields(
    tmp_path: Path,
) -> None:
    request = {
        "schema": controller.REQUEST_SCHEMA,
        "version": 1,
        "authority": controller.AUTHORITY,
        "status": "approved",
        "owner_principal_id": "owner@example.invalid",
        "expected_publication_record_sha256": "a" * 64,
        "request_id": "b" * 32,
    }
    request_path = tmp_path / controller.REQUEST_RELPATH
    request_path.write_bytes(controller._canonical(request))
    request_path.chmod(0o600)

    def _open_root() -> int:
        return os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def _read(
        root_descriptor: int,
        relpath: str,
        **_kwargs: object,
    ) -> SimpleNamespace:
        assert relpath == controller.REQUEST_RELPATH
        descriptor = os.open(relpath, os.O_RDONLY, dir_fd=root_descriptor)
        try:
            return SimpleNamespace(data=os.read(descriptor, 64 * 1024))
        finally:
            os.close(descriptor)

    admission = SimpleNamespace(
        _open_control_root=_open_root,
        _read_relative_regular=_read,
        _CONTROLLER_PATHS=SimpleNamespace(required_uid=os.geteuid()),
    )
    assert controller._load_request(admission)["request_id"] == "b" * 32

    request["unexpected"] = True
    request_path.write_bytes(controller._canonical(request))
    with pytest.raises(
        controller.PraterControllerEntrypointError,
        match="prater-controller-request-invalid",
    ):
        controller._load_request(admission)

    request.pop("unexpected")
    request["owner_principal_id"] = "nön-ascii@example.invalid"
    request_path.write_bytes(controller._canonical(request))
    with pytest.raises(
        controller.PraterControllerEntrypointError,
        match="prater-controller-request-invalid",
    ):
        controller._load_request(admission)


def test_database_secret_file_is_fixed_private_canonical_and_ephemeral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = (
        "postgresql://propertyquarry:db-pass@propertyquarry-db:5432/"
        "propertyquarry"
    )
    erasure_secret = "erasure-secret-" + "a" * 48
    payload = {
        "schema": controller.DATABASE_SECRETS_SCHEMA,
        "version": 1,
        "DATABASE_URL": database_url,
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": erasure_secret,
    }
    secret_path = tmp_path / "prater-ai-panorama-db-secrets.v1.json"
    secret_path.write_bytes(controller._canonical(payload))
    secret_path.chmod(0o400)
    monkeypatch.setattr(controller, "DATABASE_SECRETS_PATH", secret_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        raising=False,
    )

    def _read_absolute_regular(
        path: Path,
        *,
        code: str,
        maximum_bytes: int,
        required_uid: int,
        exact_mode: int,
    ) -> object:
        assert required_uid == 0
        return admission_authority._read_absolute_regular(
            path,
            code=code,
            maximum_bytes=maximum_bytes,
            required_uid=os.geteuid(),
            exact_mode=exact_mode,
        )

    admission = SimpleNamespace(
        _read_absolute_regular=_read_absolute_regular,
    )

    loaded = controller._load_database_secrets(admission)
    assert loaded == {
        "DATABASE_URL": database_url,
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": erasure_secret,
    }
    with _REAL_DATABASE_SECRET_ENVIRONMENT(admission):
        assert os.environ["DATABASE_URL"] == database_url
        assert (
            os.environ[
                "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
            ]
            == erasure_secret
        )
    assert "DATABASE_URL" not in os.environ
    assert (
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"
        not in os.environ
    )


@pytest.mark.parametrize("mutation", ("extra", "wrong-mode", "symlink"))
def test_database_secret_file_rejects_unsafe_shape_without_leaking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    database_url = (
        "postgresql://propertyquarry:do-not-leak@propertyquarry-db:5432/"
        "propertyquarry"
    )
    erasure_secret = "do-not-leak-" + "b" * 48
    payload = {
        "schema": controller.DATABASE_SECRETS_SCHEMA,
        "version": 1,
        "DATABASE_URL": database_url,
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET": erasure_secret,
    }
    target = tmp_path / "target.json"
    if mutation == "extra":
        payload["extra"] = True
    target.write_bytes(controller._canonical(payload))
    target.chmod(0o600 if mutation == "wrong-mode" else 0o400)
    secret_path = target
    if mutation == "symlink":
        secret_path = tmp_path / "secret.json"
        secret_path.symlink_to(target)
    monkeypatch.setattr(controller, "DATABASE_SECRETS_PATH", secret_path)

    def _read_absolute_regular(
        path: Path,
        *,
        code: str,
        maximum_bytes: int,
        required_uid: int,
        exact_mode: int,
    ) -> object:
        assert required_uid == 0
        return admission_authority._read_absolute_regular(
            path,
            code=code,
            maximum_bytes=maximum_bytes,
            required_uid=os.geteuid(),
            exact_mode=exact_mode,
        )

    admission = SimpleNamespace(
        _read_absolute_regular=_read_absolute_regular,
    )
    with pytest.raises(
        controller.PraterControllerEntrypointError,
    ) as captured:
        controller._load_database_secrets(admission)
    rendered = str(captured.value)
    assert database_url not in rendered
    assert erasure_secret not in rendered
    assert rendered in {
        "prater-controller-db-secrets-invalid",
        "prater-controller-db-secrets-unavailable",
    }


def test_discovery_request_loader_requires_fixed_private_canonical_file(
    tmp_path: Path,
) -> None:
    request = {
        "schema": controller.DISCOVERY_REQUEST_SCHEMA,
        "version": 1,
        "authority": controller.AUTHORITY,
        "status": "requested",
        "request_id": "b" * 32,
    }
    request_path = tmp_path / controller.DISCOVERY_REQUEST_RELPATH
    request_path.write_bytes(controller._canonical(request))
    request_path.chmod(0o600)

    def _open_root() -> int:
        return os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def _read(
        root_descriptor: int,
        relpath: str,
        **_kwargs: object,
    ) -> SimpleNamespace:
        assert relpath == controller.DISCOVERY_REQUEST_RELPATH
        descriptor = os.open(relpath, os.O_RDONLY, dir_fd=root_descriptor)
        try:
            return SimpleNamespace(data=os.read(descriptor, 64 * 1024))
        finally:
            os.close(descriptor)

    admission = SimpleNamespace(
        _open_control_root=_open_root,
        _read_relative_regular=_read,
        _CONTROLLER_PATHS=SimpleNamespace(required_uid=os.geteuid()),
    )

    assert controller._load_discovery_request(admission) == {
        "request_id": "b" * 32,
    }

    request["status"] = "approved"
    request_path.write_bytes(controller._canonical(request))
    with pytest.raises(
        controller.PraterControllerEntrypointError,
        match="prater-controller-discovery-request-invalid",
    ):
        controller._load_discovery_request(admission)


def test_expected_bindings_are_closed_over_prater_and_trusted_context() -> None:
    captured: dict[str, object] = {}

    def _expected(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    components = SimpleNamespace(
        admission=SimpleNamespace(AiPanoramaInstallExpectedBindings=_expected),
        release=_release_module(),
    )
    request = {
        "owner_principal_id": "private-owner@example.invalid",
        "expected_publication_record_sha256": "6" * 64,
        "request_id": "7" * 32,
    }

    controller._expected_bindings(components, request, _trusted())

    assert captured["owner_principal_id"] == request["owner_principal_id"]
    assert captured["actor_principal_id"] == _trusted().actor_principal_id
    assert captured["source_ref"] == "property-scout:1807240910"
    assert captured["expected_publication_record_sha256"] == "6" * 64
    assert captured["compose_plan_sha256"] == "e" * 64
    assert captured["web_image_id"] == "sha256:" + "2" * 64
    assert captured["keyring_sha256"] == "3" * 64


def test_run_consumes_fixed_permit_and_writes_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    release = _release_module()

    def _expected(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def _consume(permit_relpath: str, expected: object) -> SimpleNamespace:
        captured["permit_relpath"] = permit_relpath
        captured["expected"] = expected
        return SimpleNamespace(permit_sha256="8" * 64)

    admission = SimpleNamespace(
        AiPanoramaInstallExpectedBindings=_expected,
        load_ai_panorama_install_trusted_context=_trusted,
        consume_ai_panorama_install_permit=_consume,
    )

    def _release(_verified: object, *, apply: bool) -> dict[str, object]:
        assert apply is True
        return {
            "status": "released",
            "release_eligible": True,
            "private_values_redacted": True,
        }

    release.run_prater_ai_panorama_release = _release
    components = SimpleNamespace(admission=admission, release=release)

    @contextlib.contextmanager
    def _db_loaded(_admission: object):  # type: ignore[no-untyped-def]
        captured["db_secret_loaded"] = True
        yield

    monkeypatch.setattr(
        controller,
        "_database_secret_environment",
        _db_loaded,
    )
    monkeypatch.setattr(controller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(controller.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        controller,
        "_require_attested_image_entrypoint",
        lambda _expected_path: None,
    )
    monkeypatch.setattr(controller, "_components", lambda: components)
    monkeypatch.setattr(
        controller,
        "_load_request",
        lambda _admission: {
            "owner_principal_id": "private-owner@example.invalid",
            "expected_publication_record_sha256": "6" * 64,
            "request_id": "7" * 32,
        },
    )
    monkeypatch.setattr(
        controller,
        "_require_terminal_absent",
        lambda _admission, _relpath: None,
    )
    monkeypatch.setattr(
        controller,
        "_write_terminal_receipt",
        lambda _admission, *, relpath, payload: (
            captured.update({"terminal_relpath": relpath, "terminal": payload})
            or "9" * 64
        ),
    )

    result = controller.run()

    assert captured["permit_relpath"] == controller.PERMIT_RELPATH
    assert captured["db_secret_loaded"] is True
    assert captured["terminal_relpath"] == f"terminal-{'7' * 32}.v1.json"
    assert captured["terminal"]["status"] == "committed"
    assert result["status"] == "committed"
    assert result["terminal_receipt_sha256"] == "9" * 64
    assert "private-owner@example.invalid" not in json.dumps(result)


def test_preflight_verifies_without_consuming_or_writing_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    release = _release_module()

    def _expected(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def _verify(permit_relpath: str, expected: object) -> SimpleNamespace:
        captured["permit_relpath"] = permit_relpath
        captured["expected"] = expected
        return SimpleNamespace(permit_sha256="8" * 64)

    admission = SimpleNamespace(
        AiPanoramaInstallExpectedBindings=_expected,
        load_ai_panorama_install_trusted_context=_trusted,
        verify_ai_panorama_install_permit=_verify,
        consume_ai_panorama_install_permit=lambda *_args: pytest.fail(
            "preflight must not consume the permit"
        ),
    )

    def _preflight(verified: object) -> dict[str, object]:
        captured["verified"] = verified
        return {
            "status": "preflight_passed",
            "nonce_consumed": False,
            "database_access_performed": False,
        }

    release.run_prater_ai_panorama_artifact_preflight = _preflight
    monkeypatch.setattr(
        controller,
        "_database_secret_environment",
        lambda _admission: pytest.fail(
            "preflight must not load or mount DB secrets"
        ),
    )
    monkeypatch.setattr(controller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(controller.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        controller,
        "_require_attested_image_entrypoint",
        lambda expected_path: captured.setdefault("entrypoint", expected_path),
    )
    monkeypatch.setattr(
        controller,
        "_components",
        lambda: SimpleNamespace(admission=admission, release=release),
    )
    monkeypatch.setattr(controller, "_load_request", lambda _admission: _request())
    monkeypatch.setattr(
        controller,
        "_write_terminal_receipt",
        lambda *_args, **_kwargs: pytest.fail(
            "preflight must not write a terminal receipt"
        ),
    )

    result = controller.run_preflight()

    assert captured["entrypoint"] == controller.PREFLIGHT_ENTRYPOINT_PATH
    assert captured["permit_relpath"] == controller.PERMIT_RELPATH
    assert result["status"] == "preflight-passed"
    assert result["nonce_consumed"] is False
    assert result["database_access_performed"] is False


def test_record_discovery_is_no_argument_read_only_and_non_authorizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    release = _release_module()

    def _discover() -> dict[str, object]:
        captured["discovery_called"] = True
        return {
            "status": "record-discovered",
            "owner_principal_id": "private-owner@example.invalid",
            "expected_publication_record_sha256": "a" * 64,
            "database_mutation_performed": False,
            "release_authorized": False,
        }

    release.discover_prater_ai_panorama_publication_record = _discover
    admission = SimpleNamespace(
        consume_ai_panorama_install_permit=lambda *_args: pytest.fail(
            "record discovery must not consume or sign a permit"
        )
    )
    monkeypatch.setattr(controller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(controller.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        controller,
        "_require_attested_image_entrypoint",
        lambda expected_path: captured.setdefault("entrypoint", expected_path),
    )
    monkeypatch.setattr(
        controller,
        "_components",
        lambda: SimpleNamespace(admission=admission, release=release),
    )

    @contextlib.contextmanager
    def _db_loaded(_admission: object):  # type: ignore[no-untyped-def]
        captured["db_secret_loaded"] = True
        yield

    monkeypatch.setattr(
        controller,
        "_database_secret_environment",
        _db_loaded,
    )
    monkeypatch.setattr(
        controller,
        "_load_discovery_request",
        lambda _admission: {
            "request_id": "7" * 32,
        },
    )

    result = controller.run_record_discovery()

    assert captured["entrypoint"] == controller.DISCOVERY_ENTRYPOINT_PATH
    assert captured["db_secret_loaded"] is True
    assert captured["discovery_called"] is True
    assert result["owner_principal_id"] == "private-owner@example.invalid"
    assert result["status"] == "discovered"
    assert result["expected_publication_record_sha256"] == "a" * 64
    assert result["database_mutation_performed"] is False
    assert result["release_authorized"] is False
    assert result["private_projection"] is True


def test_record_discovery_rejects_non_ascii_owner_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release_module()
    release.discover_prater_ai_panorama_publication_record = lambda: {
        "status": "record-discovered",
        "owner_principal_id": "nön-ascii@example.invalid",
        "expected_publication_record_sha256": "a" * 64,
        "database_mutation_performed": False,
        "release_authorized": False,
    }
    monkeypatch.setattr(controller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(controller.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        controller,
        "_require_attested_image_entrypoint",
        lambda _expected_path: None,
    )
    monkeypatch.setattr(
        controller,
        "_components",
        lambda: SimpleNamespace(
            admission=SimpleNamespace(),
            release=release,
        ),
    )
    monkeypatch.setattr(
        controller,
        "_load_discovery_request",
        lambda _admission: {"request_id": "7" * 32},
    )

    with pytest.raises(
        controller.PraterControllerEntrypointError,
        match="prater-controller-record-discovery-failed",
    ):
        controller.run_record_discovery()


def test_commit_ambiguity_writes_recovery_required_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    release = _release_module()
    failure = RuntimeError("ambiguous")
    failure.code = "ai-panorama-transaction-ambiguous"  # type: ignore[attr-defined]
    failure.commit_outcome_ambiguous = True  # type: ignore[attr-defined]
    admission = SimpleNamespace(
        AiPanoramaInstallExpectedBindings=lambda **kwargs: SimpleNamespace(
            **kwargs
        ),
        load_ai_panorama_install_trusted_context=_trusted,
        consume_ai_panorama_install_permit=lambda *_args: SimpleNamespace(
            permit_sha256="8" * 64
        ),
    )
    release.run_prater_ai_panorama_release = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure)
    )
    monkeypatch.setattr(controller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(controller.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        controller,
        "_require_attested_image_entrypoint",
        lambda _expected_path: None,
    )
    monkeypatch.setattr(
        controller,
        "_components",
        lambda: SimpleNamespace(admission=admission, release=release),
    )
    monkeypatch.setattr(controller, "_load_request", lambda _admission: _request())
    monkeypatch.setattr(
        controller,
        "_require_terminal_absent",
        lambda _admission, _relpath: None,
    )
    monkeypatch.setattr(
        controller,
        "_write_terminal_receipt",
        lambda _admission, *, relpath, payload: (
            captured.update({"relpath": relpath, "payload": payload}) or "9" * 64
        ),
    )

    with pytest.raises(
        controller.PraterControllerEntrypointError,
        match="ai-panorama-transaction-ambiguous",
    ):
        controller.run()

    assert captured["payload"]["status"] == "recovery-required"
    assert captured["payload"]["error"] == "ai-panorama-transaction-ambiguous"


def test_malformed_return_after_inner_release_requires_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release_module()
    admission = SimpleNamespace(
        AiPanoramaInstallExpectedBindings=lambda **kwargs: SimpleNamespace(
            **kwargs
        ),
        load_ai_panorama_install_trusted_context=_trusted,
        consume_ai_panorama_install_permit=lambda *_args: SimpleNamespace(
            permit_sha256="8" * 64
        ),
    )
    release.run_prater_ai_panorama_release = lambda *_args, **_kwargs: {
        "status": "malformed-after-inner-commit",
        "release_eligible": False,
    }
    terminal_writes: list[dict[str, object]] = []
    monkeypatch.setattr(controller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(controller.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        controller,
        "_require_attested_image_entrypoint",
        lambda _expected_path: None,
    )
    monkeypatch.setattr(
        controller,
        "_components",
        lambda: SimpleNamespace(admission=admission, release=release),
    )
    monkeypatch.setattr(controller, "_load_request", lambda _admission: _request())
    monkeypatch.setattr(
        controller,
        "_require_terminal_absent",
        lambda _admission, _relpath: None,
    )
    monkeypatch.setattr(
        controller,
        "_write_terminal_receipt",
        lambda _admission, *, relpath, payload: (
            terminal_writes.append(dict(payload)) or "9" * 64
        ),
    )

    with pytest.raises(
        controller.PraterControllerEntrypointError,
        match="prater-controller-recovery-required",
    ):
        controller.run()

    assert terminal_writes == []


def test_committed_release_terminal_write_failure_never_writes_false_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release_module()
    admission = SimpleNamespace(
        AiPanoramaInstallExpectedBindings=lambda **kwargs: SimpleNamespace(
            **kwargs
        ),
        load_ai_panorama_install_trusted_context=_trusted,
        consume_ai_panorama_install_permit=lambda *_args: SimpleNamespace(
            permit_sha256="8" * 64
        ),
    )
    release.run_prater_ai_panorama_release = lambda *_args, **_kwargs: {
        "status": "released",
        "release_eligible": True,
    }
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(controller.os, "geteuid", lambda: 0)
    monkeypatch.setattr(controller.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        controller,
        "_require_attested_image_entrypoint",
        lambda _expected_path: None,
    )
    monkeypatch.setattr(
        controller,
        "_components",
        lambda: SimpleNamespace(admission=admission, release=release),
    )
    monkeypatch.setattr(controller, "_load_request", lambda _admission: _request())
    monkeypatch.setattr(
        controller,
        "_require_terminal_absent",
        lambda _admission, _relpath: None,
    )

    def _terminal(
        _admission: object,
        *,
        relpath: str,
        payload: dict[str, object],
    ) -> str:
        calls.append(dict(payload))
        raise OSError("injected terminal write failure")

    monkeypatch.setattr(controller, "_write_terminal_receipt", _terminal)

    with pytest.raises(
        controller.PraterControllerEntrypointError,
        match="prater-controller-recovery-required",
    ):
        controller.run()

    assert len(calls) == 1
    assert calls[0]["status"] == "committed"


def test_main_refuses_all_arguments_before_release(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(controller.sys, "argv", ["controller", "--permit", "x"])
    monkeypatch.setattr(
        controller,
        "run",
        lambda: pytest.fail("argument-bearing invocation must not run"),
    )

    assert controller.main() == 2
    value = json.loads(capsys.readouterr().out)
    assert value["error"] == "prater-controller-arguments-forbidden"
