from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import check_property_security_posture as security_posture


ROOT = Path(__file__).resolve().parents[1]


def _compose() -> str:
    return (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")


def _writer_topology_text() -> str:
    return (
        ROOT
        / "config/release/propertyquarry_deploy_writer_topology.v1.json"
    ).read_text(encoding="utf-8")


def _writer_topology() -> dict[str, object]:
    payload = json.loads(_writer_topology_text())
    assert isinstance(payload, dict)
    return payload


def test_current_durable_worker_passes_positive_security_contract() -> None:
    compose = _compose()

    assert security_posture._durable_worker_security_failures(
        security_posture._compose_service_block(compose, "propertyquarry-worker"),
        api=security_posture._compose_service_block(compose, "propertyquarry-api"),
    ) == []


def test_security_posture_receipt_includes_worker_and_render_database_controls() -> None:
    receipt = security_posture.build_security_posture_receipt()

    assert receipt["status"] == "pass"
    assert receipt["failure_count"] == 0
    required_checks = receipt["required_checks"]
    assert isinstance(required_checks, list)
    assert "compose_runtime_privilege_boundaries" in required_checks
    assert "durable_property_worker_hardening" in required_checks
    assert "render_database_isolation" in required_checks


def test_resolved_worker_security_contract_checks_the_effective_model() -> None:
    services = security_posture._resolved_compose_services(_compose())
    worker = deepcopy(services["propertyquarry-worker"])
    worker["read_only"] = False
    volumes = worker["volumes"]
    assert isinstance(volumes, list)
    volumes.append(
        {
            "source": "/var/run/docker.sock",
            "target": "/var/run/docker.sock",
            "type": "bind",
        }
    )

    failures = security_posture._resolved_durable_worker_security_failures(
        worker,
        api=services["propertyquarry-api"],
    )

    assert any("read-only root filesystem" in failure for failure in failures)
    assert any("must mount only" in failure for failure in failures)


def test_resolved_runtime_contract_rejects_rogue_privileged_service() -> None:
    services = security_posture._resolved_compose_services(_compose())
    services["candidate-helper"] = {"privileged": False}

    failures = security_posture._resolved_compose_runtime_privilege_failures(
        services
    )

    assert any("contain exactly" in failure for failure in failures)
    assert any(
        "candidate-helper must not set runtime privilege boundary privileged"
        in failure
        for failure in failures
    )


def _security_receipt_with_compose(
    monkeypatch: pytest.MonkeyPatch,
    compose: str,
) -> dict[str, object]:
    return _security_receipt_with_overrides(
        monkeypatch,
        {"docker-compose.property.yml": compose},
    )


def _security_receipt_with_overrides(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> dict[str, object]:
    original_read = security_posture._read

    def read_override(path: str) -> str:
        if path in overrides:
            return overrides[path]
        return original_read(path)

    monkeypatch.setattr(
        security_posture,
        "_read",
        read_override,
    )
    return security_posture.build_security_posture_receipt()


def test_security_posture_rejects_duplicate_worker_property_false_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = (
        '    read_only: true\n'
        '    restart: "${PROPERTYQUARRY_WORKER_RESTART_POLICY'
    )
    compose = _compose()
    assert marker in compose
    compose = compose.replace(
        marker,
        '    read_only: true\n'
        '    read_only: false\n'
        '    restart: "${PROPERTYQUARRY_WORKER_RESTART_POLICY',
        1,
    )

    permissive_resolution = yaml.safe_load(compose)
    assert (
        permissive_resolution["services"]["propertyquarry-worker"]["read_only"]
        is False
    )
    assert security_posture._durable_worker_security_failures(
        security_posture._compose_service_block(
            compose,
            "propertyquarry-worker",
        ),
        api=security_posture._compose_service_block(
            compose,
            "propertyquarry-api",
        ),
    ) == []

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "failed strict Docker Compose resolution" in failure
        for failure in receipt["failures"]
    )


def test_security_posture_rejects_duplicate_worker_service_false_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = _compose()
    marker = "\nvolumes:\n"
    assert marker in compose
    compose = compose.replace(
        marker,
        "\n"
        "  propertyquarry-worker:\n"
        "    read_only: false\n"
        "\n"
        "volumes:\n",
        1,
    )

    permissive_resolution = yaml.safe_load(compose)
    assert permissive_resolution["services"]["propertyquarry-worker"] == {
        "read_only": False
    }
    first_worker = security_posture._compose_service_block(
        compose,
        "propertyquarry-worker",
    )
    assert security_posture._service_scalar(first_worker, "read_only") == "true"

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "failed strict Docker Compose resolution" in failure
        for failure in receipt["failures"]
    )


def test_security_posture_rejects_identical_duplicate_render_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = _compose()
    render_block = security_posture._compose_service_block(
        compose,
        "propertyquarry-render-tools",
    )
    marker = "      TZ: Europe/Vienna\n"
    assert render_block.count(marker) == 1
    compose = compose.replace(
        render_block,
        render_block.replace(marker, marker + marker, 1),
        1,
    )

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "failed strict Docker Compose resolution" in failure
        for failure in receipt["failures"]
    )


@pytest.mark.parametrize(
    "duplicate_level",
    ["root", "nested_environment"],
)
def test_security_posture_rejects_duplicate_keys_at_other_mapping_depths(
    monkeypatch: pytest.MonkeyPatch,
    duplicate_level: str,
) -> None:
    compose = _compose()
    if duplicate_level == "root":
        compose += "\nservices: {}\n"
    else:
        marker = "      EA_ROLE: worker"
        assert marker in compose
        compose = compose.replace(
            marker,
            marker + "\n      EA_ROLE: generic",
            1,
        )

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "failed strict Docker Compose resolution" in failure
        for failure in receipt["failures"]
    )


@pytest.mark.parametrize(
    ("resolved_payload", "expected_failure"),
    [
        ("[]\n", "root must be a JSON object"),
        ('{"services": []}\n', "services must be an object"),
        (
            '{"services": {"propertyquarry-worker": true}}\n',
            "service entries must be named objects",
        ),
    ],
)
def test_strict_compose_resolution_rejects_non_mapping_shapes(
    monkeypatch: pytest.MonkeyPatch,
    resolved_payload: str,
    expected_failure: str,
) -> None:
    monkeypatch.setattr(
        security_posture.subprocess,
        "run",
        lambda *args, **kwargs: security_posture.subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=resolved_payload,
            stderr="",
        ),
    )

    with pytest.raises(
        security_posture.SecurityPostureConfigError,
        match=expected_failure,
    ):
        security_posture._resolved_compose_services(_compose())


def test_security_posture_rejects_yaml_merge_privilege_false_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = (
        "x-propertyquarry-unsafe: &propertyquarry-unsafe\n"
        "  privileged: true\n"
        "  network_mode: host\n"
        + _compose().replace(
            "  propertyquarry-worker:\n",
            "  propertyquarry-worker:\n"
            "    <<: *propertyquarry-unsafe\n",
            1,
        )
    )
    resolved = yaml.safe_load(compose)["services"]["propertyquarry-worker"]
    assert resolved["privileged"] is True
    assert resolved["network_mode"] == "host"

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "must not use YAML merge keys" in failure
        for failure in receipt["failures"]
    )


@pytest.mark.parametrize(
    "include_key",
    ["include", '"include"', "'include'"],
)
def test_security_posture_rejects_uninspected_compose_include_without_resolving_it(
    monkeypatch: pytest.MonkeyPatch,
    include_key: str,
) -> None:
    compose = (
        f"{include_key}:\n"
        "  - path: ./candidate-privileged-compose.yml\n"
        + _compose()
    )
    monkeypatch.setattr(
        security_posture.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "invalid external Compose directives must not be resolved"
        ),
    )

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "must not include external Compose documents" in failure
        for failure in receipt["failures"]
    )


def test_security_posture_rejects_bom_prefixed_include_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = (
        "\ufeffinclude:\n"
        "  - path: /definitely-does-not-exist-propertyquarry-audit\n"
        + _compose()
    )
    monkeypatch.setattr(
        security_posture.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "BOM-prefixed external includes must fail before Compose resolution"
        ),
    )

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "must not contain BOM or control characters" in failure
        for failure in receipt["failures"]
    )


def test_security_posture_rejects_variable_indentation_extends_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = _compose()
    api_block = security_posture._compose_service_block(
        compose,
        "propertyquarry-api",
    )
    api_lines = api_block.splitlines(keepends=True)
    reindented_api = api_lines[0] + "".join(
        " " + line if line.startswith("    ") else line
        for line in api_lines[1:]
    )
    reindented_api = reindented_api.replace(
        "  propertyquarry-api:\n",
        "  propertyquarry-api:\n"
        "     extends:\n"
        "       file: /definitely-does-not-exist-propertyquarry-audit\n"
        "       service: privileged-api\n",
        1,
    )
    compose = compose.replace(api_block, reindented_api, 1)
    monkeypatch.setattr(
        security_posture.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "external service inheritance must fail before Compose resolution"
        ),
    )

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "must not use external service inheritance" in failure
        for failure in receipt["failures"]
    )


@pytest.mark.parametrize(
    "external_directive",
    [
        (
            "&loader include:\n"
            "  - path: /definitely-does-not-exist-propertyquarry-audit\n"
        ),
        (
            "x-loader: &loader include\n"
            "*loader:\n"
            "  - path: /definitely-does-not-exist-propertyquarry-audit\n"
        ),
    ],
)
def test_security_posture_rejects_anchored_or_aliased_external_loader_keys(
    monkeypatch: pytest.MonkeyPatch,
    external_directive: str,
) -> None:
    compose = external_directive + _compose()
    monkeypatch.setattr(
        security_posture.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "anchored or aliased external loaders must fail before resolution"
        ),
    )

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        "must not use YAML anchors or aliases" in failure
        for failure in receipt["failures"]
    )


@pytest.mark.parametrize(
    ("service_name", "runtime_override", "expected_failure"),
    [
        ("propertyquarry-api", "    privileged: true\n", "privileged"),
        (
            "propertyquarry-worker",
            "    cap_add:\n      - NET_ADMIN\n",
            "cap_add",
        ),
        ("propertyquarry-scheduler", "    pid: host\n", "pid"),
        (
            "propertyquarry-render-tools",
            "    security_opt:\n      - seccomp:unconfined\n",
            "security_opt",
        ),
        (
            "propertyquarry-db",
            '    "user": !!str 0\n',
            "explicit YAML tags",
        ),
        (
            "propertyquarry-db",
            "    user: 0:0\n",
            "fixed non-root identity",
        ),
        (
            "propertyquarry-api",
            "    use_api_socket: true\n",
            "use_api_socket",
        ),
        (
            "propertyquarry-api",
            "    extends:\n"
            "      file: ./candidate-privileged-compose.yml\n"
            "      service: privileged-api\n",
            "extends",
        ),
        (
            "propertyquarry-scheduler",
            "    post_start:\n"
            "      - command: /candidate/escalate\n"
            "        privileged: true\n",
            "post_start",
        ),
        (
            "propertyquarry-render-tools",
            "    provider:\n"
            "      type: candidate-runtime-provider\n",
            "provider",
        ),
    ],
)
def test_security_posture_rejects_service_privilege_overrides(
    monkeypatch: pytest.MonkeyPatch,
    service_name: str,
    runtime_override: str,
    expected_failure: str,
) -> None:
    marker = f"  {service_name}:\n"
    compose = _compose()
    assert marker in compose
    compose = compose.replace(marker, marker + runtime_override, 1)

    receipt = _security_receipt_with_compose(monkeypatch, compose)

    assert receipt["status"] == "fail"
    assert any(
        expected_failure in failure
        for failure in receipt["failures"]
    )


@pytest.mark.parametrize(
    ("old", "new", "expected_failure"),
    [
        ("    read_only: true", "    read_only: false", "read-only root filesystem"),
        ("      - ALL", "      - NET_ADMIN", "drop all Linux capabilities"),
        (
            '      - "no-new-privileges:true"',
            '      - "seccomp:unconfined"',
            "no-new-privileges",
        ),
        (
            "    read_only: true",
            "    read_only: true\n    privileged: true",
            "must not be privileged",
        ),
        (
            "    read_only: true",
            "    read_only: true\n    network_mode: host",
            "must not use host networking",
        ),
        (
            "    read_only: true",
            '    read_only: true\n    ports:\n      - "8090:8090"',
            "must not publish host ports",
        ),
    ],
)
def test_durable_worker_security_contract_rejects_removed_process_boundaries(
    old: str,
    new: str,
    expected_failure: str,
) -> None:
    compose = _compose()
    api = security_posture._compose_service_block(compose, "propertyquarry-api")
    worker = security_posture._compose_service_block(
        compose,
        "propertyquarry-worker",
    )
    assert old in worker
    worker = worker.replace(old, new, 1)

    failures = security_posture._durable_worker_security_failures(
        worker,
        api=api,
    )

    assert any(expected_failure in failure for failure in failures)


def test_durable_worker_security_contract_rejects_render_environment_bundle() -> None:
    compose = _compose()
    api = security_posture._compose_service_block(compose, "propertyquarry-api")
    worker = security_posture._compose_service_block(
        compose,
        "propertyquarry-worker",
    )
    worker = worker.replace(
        "    env_file:\n      - .env",
        "    env_file:\n"
        "      - path: ./state/runtime/property_scene_video_shared.env\n"
        "        required: false\n"
        "      - .env",
        1,
    )

    failures = security_posture._durable_worker_security_failures(
        worker,
        api=api,
    )

    assert any("optional render environment bundles" in failure for failure in failures)


def test_durable_worker_security_contract_requires_property_only_postgres_role() -> None:
    compose = _compose()
    api = security_posture._compose_service_block(compose, "propertyquarry-api")
    worker = security_posture._compose_service_block(
        compose,
        "propertyquarry-worker",
    )
    worker = worker.replace(
        '      PROPERTYQUARRY_WORKER_PROFILE: "property_only"',
        '      PROPERTYQUARRY_WORKER_PROFILE: "generic"',
        1,
    ).replace(
        '      EA_STORAGE_BACKEND: "postgres"',
        '      EA_STORAGE_BACKEND: "memory"',
        1,
    )

    failures = security_posture._durable_worker_security_failures(
        worker,
        api=api,
    )

    assert any("PROPERTYQUARRY_WORKER_PROFILE=property_only" in failure for failure in failures)
    assert any("EA_STORAGE_BACKEND=postgres" in failure for failure in failures)


def test_current_render_bridge_is_explicitly_database_isolated() -> None:
    compose = _compose()

    assert security_posture._render_non_writer_security_failures(
        security_posture._compose_service_block(
            compose,
            "propertyquarry-render-tools",
        ),
        writer_topology=_writer_topology(),
    ) == []


def test_render_database_isolation_rejects_credential_or_writer_classification() -> None:
    render = security_posture._compose_service_block(
        _compose(),
        "propertyquarry-render-tools",
    ).replace(
        '      DATABASE_URL: ""',
        '      DATABASE_URL: "${DATABASE_URL}"',
        1,
    )
    topology = deepcopy(_writer_topology())
    target = topology["target"]
    assert isinstance(target, dict)
    render_topology = target["render"]
    assert isinstance(render_topology, dict)
    render_topology["database_writer"] = True

    failures = security_posture._render_non_writer_security_failures(
        render,
        writer_topology=topology,
    )

    assert any("explicitly blank DATABASE_URL" in failure for failure in failures)
    assert any("current render bridge as a non-writer" in failure for failure in failures)


@pytest.mark.parametrize(
    ("topology_kind", "expected_failure"),
    [
        ("duplicate", "duplicate object key"),
        ("non_finite", "non-finite JSON constant"),
        ("non_object", "root must be a JSON object"),
    ],
)
def test_security_posture_strictly_parses_writer_topology(
    monkeypatch: pytest.MonkeyPatch,
    topology_kind: str,
    expected_failure: str,
) -> None:
    topology = _writer_topology_text()
    if topology_kind == "duplicate":
        marker = '"database_writer": false'
        assert marker in topology
        topology = topology.replace(
            marker,
            '"database_writer": true,\n'
            '      "database_writer": false',
            1,
        )
        permissive_payload = json.loads(topology)
        assert permissive_payload["target"]["render"]["database_writer"] is False
    elif topology_kind == "non_finite":
        marker = '"external_database_writers": []'
        assert marker in topology
        topology = topology.replace(
            marker,
            '"external_database_writers": [NaN]',
            1,
        )
        assert json.loads(topology)["external_database_writers"]
    else:
        topology = "[]\n"

    receipt = _security_receipt_with_overrides(
        monkeypatch,
        {
            "config/release/"
            "propertyquarry_deploy_writer_topology.v1.json": topology
        },
    )

    assert receipt["status"] == "fail"
    assert any(
        expected_failure in failure
        for failure in receipt["failures"]
    )
