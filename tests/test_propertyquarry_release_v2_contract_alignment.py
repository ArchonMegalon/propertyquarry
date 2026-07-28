"""Cross-module invariants for the isolated PropertyQuarry lifecycle-v2 contracts."""

from scripts import propertyquarry_controller_response_frame as response_frame
from scripts import propertyquarry_release_evidence as evidence
from scripts import propertyquarry_release_installation_model as installation
from scripts import propertyquarry_release_lifecycle_model as lifecycle
from scripts import propertyquarry_release_preflight_policy as preflight_policy
from scripts import propertyquarry_release_request_authority_model as request_authority
from scripts import propertyquarry_release_supervisor_model as supervisor
from scripts import propertyquarry_release_socket_transport_model as socket_transport


def test_success_phase_order_is_identical_across_lifecycle_and_evidence() -> None:
    assert tuple(phase.value for phase in lifecycle.SUCCESS_PATH) == evidence.SUCCESS_STATES


def test_complete_fencing_domain_is_identical_across_lifecycle_and_evidence() -> None:
    lifecycle_kinds = {kind.value for kind in lifecycle.REQUIRED_RESOURCE_KINDS}
    evidence_kinds = set(evidence.RESOURCE_KINDS)
    preflight_kinds = set(preflight_policy.RESOURCE_KINDS)

    assert lifecycle_kinds == evidence_kinds == preflight_kinds
    assert set(evidence.TERMINAL_RESOURCE_EVIDENCE) == evidence_kinds
    assert set(evidence.FIXED_OUTCOMES) == evidence_kinds


def test_external_operations_and_workflow_boundary_do_not_drift() -> None:
    assert set(lifecycle.EXTERNAL_OPERATIONS) == response_frame.OPERATIONS
    assert lifecycle.WORKFLOW_OPERATIONS == (
        "release-preflight",
        "release-run",
    )
    assert "reconcile-run" in lifecycle.EXTERNAL_OPERATIONS
    assert "reconcile-run" not in lifecycle.WORKFLOW_OPERATIONS
    assert tuple(
        operation.value for operation in request_authority.Operation
    ) == lifecycle.WORKFLOW_OPERATIONS
    assert set(operation.value for operation in request_authority.Operation) < (
        response_frame.OPERATIONS
    )
    assert supervisor.response_frame.OPERATIONS == response_frame.OPERATIONS


def test_all_repository_wire_contracts_are_explicitly_version_two_and_distinct() -> None:
    assert evidence.VERSION == 2
    assert response_frame.LIFECYCLE_RESPONSE_VERSION == 2
    assert preflight_policy.VERSION == 2
    assert installation.VERSION == 2
    assert lifecycle.SCHEMA.endswith(".v2")
    assert evidence.SCHEMA != lifecycle.SCHEMA
    assert response_frame.LIFECYCLE_RESPONSE_SCHEMA not in {
        evidence.SCHEMA,
        lifecycle.SCHEMA,
    }
    assert request_authority.REQUEST_SCHEMA.endswith(".v2")
    assert request_authority.RESPONSE_SCHEMA.endswith(".v2")
    assert request_authority.REQUEST_SCHEMA != request_authority.RESPONSE_SCHEMA
    assert socket_transport.describe_contract()["version"] == 2
    assert socket_transport.describe_contract()["authoritative"] is False
    assert {
        request_authority.REQUEST_SCHEMA,
        request_authority.RESPONSE_SCHEMA,
    }.isdisjoint(
        {
            evidence.SCHEMA,
            lifecycle.SCHEMA,
            response_frame.LIFECYCLE_RESPONSE_SCHEMA,
        }
    )


def test_supervisor_contract_is_fixed_outside_the_candidate_checkout() -> None:
    assert installation.ROLE_BY_NAME["supervisor-executable"].path == (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-supervisor-v2"
    )
    assert socket_transport.INSTALLED_SOCKET_PATH == (
        "/run/propertyquarry-release-control-v2/request.sock"
    )
    assert supervisor.INSTALLED_CONTROLLER_EXECUTABLE == (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-controller-v2"
    )
    assert installation.ROLE_BY_NAME["controller-executable"].path == (
        supervisor.INSTALLED_CONTROLLER_EXECUTABLE
    )
    assert supervisor.INSTALLED_CONTROLLER_CONFIG == (
        "/etc/propertyquarry-release-control/controller-v2.json"
    )
    assert supervisor.INSTALLED_CONTRACT_ID == (
        "propertyquarry.release.installed-controller-v2"
    )
    assert request_authority.describe_contract()["authoritative"] is False
    assert supervisor._DIGEST.fullmatch(
        request_authority.sha256_digest(b"request-transport-profile")
    )
    assert supervisor.describe_contract()["full_verification"] == (
        "typed-event-request-frame-exact-installed-policy-bound-receipt"
    )
    assert supervisor.describe_contract()["modeled_role"] == (
        "systemd-supervisor-broker-inner-controller-child"
    )
    assert supervisor.describe_contract()["workflow_client_transport"] == (
        "separate-unix-socket-model"
    )
    assert preflight_policy.AUTHORITATIVE is False
    assert preflight_policy.PERFORMS_EFFECTS is False
    assert len(preflight_policy.REQUIRED_CHECK_IDS) == 28
    assert installation.AUTHORITATIVE is False
    assert installation.PERFORMS_WRITES is False
    assert installation.ROLE_BY_NAME["controller-config"].path == (
        supervisor.INSTALLED_CONTROLLER_CONFIG
    )


def test_policy_and_replay_bindings_are_exact_across_authority_layers() -> None:
    request_contract = request_authority.describe_contract()
    policy_contract = request_contract["root_policy_digest"]
    admission_contract = request_contract["admission_binding_digest"]
    lifecycle_contract = lifecycle.describe_contract()

    assert request_authority.ROOT_POLICY_SCHEMA == (
        "propertyquarry.release-root-policy.v2"
    )
    assert request_authority.ROOT_POLICY_DIGEST_DOMAIN == (
        b"propertyquarry.release-root-policy-digest.v2\0"
    )
    assert policy_contract["length_framing"] == (
        "unsigned-64-bit-big-endian-canonical-json-length"
    )
    assert {
        "replay-record",
        "ready-preflight",
        "admission-request",
        "admission-result",
        "signed-response-payload",
        "persisted-state",
    } == set(policy_contract["bindings"])
    assert admission_contract["immutable_predecessor"] == (
        "ready-preflight-observed-head"
    )
    assert set(admission_contract["excludes"]) == {
        "release-evaluated-at",
        "observed-current-head",
    }
    assert lifecycle_contract["replay_command_context"] == (
        "binding-policy-controller-resources-epoch-lease-and-effect-inputs;"
        "clock-and-cas-excluded"
    )
    assert supervisor.describe_contract()["expected_policy_digest"] == (
        "required-trusted-root-owned-installed-input-not-request-derived"
    )


def test_terminal_evidence_is_closed_per_resource_and_response_class() -> None:
    assert evidence.REQUIRED_EVIDENCE["sealed-final"].issuperset(
        evidence.TERMINAL_RESOURCE_EVIDENCE.values()
    )
    assert response_frame.EXPECTED_CLASSES_BY_EXIT[
        response_frame.ControllerExit.SUCCESS
    ] == frozenset({"ready", "sealed-final"})
    assert response_frame.EXPECTED_CLASSES_BY_EXIT[
        response_frame.ControllerExit.ROLLED_BACK
    ] == frozenset({"rolled-back"})
    assert response_frame.EXPECTED_CLASSES_BY_EXIT[
        response_frame.ControllerExit.CONTAINED_FAILED_OR_RECONCILIATION_REQUIRED
    ] == frozenset({"contained-failed"})
