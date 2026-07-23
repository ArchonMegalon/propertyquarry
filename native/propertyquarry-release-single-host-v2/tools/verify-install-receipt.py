#!/usr/bin/env python3
"""Verify installer receipts against the independently verified package."""

from __future__ import annotations

import argparse
import base64
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any

from cryptography.exceptions import InvalidSignature


MODULE_ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_single_host_package_for_receipt",
    MODULE_ROOT / "tools" / "package.py",
)
assert SPEC is not None and SPEC.loader is not None
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)

INSTALL_RECEIPT_DOMAIN = (
    b"propertyquarry.release-control.single-host-install-receipt-signature.v2\x00"
)
ACTIVATION_CANARY_DOMAIN = (
    b"propertyquarry.release-control.single-host-activation-canary-"
    b"receipt-signature.v2\x00"
)
RECEIPT_ANCHOR_MEMBER = (
    "payload/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem"
)
BUILD_RECEIPT_MEMBER = (
    "payload/etc/propertyquarry-release-single-host-v2/"
    "native-build-receipt.v2.json"
)
SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{86}$")


def fail(code: str) -> None:
    raise package.PackageFailure(code)


def exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        fail(f"{label}-shape-invalid")


def verify_wire(
    raw: bytes, verified: Any
) -> tuple[dict[str, Any], str]:
    wrapper = package.parse_strict_json(raw, "installer-receipt")
    exact_keys(
        wrapper,
        {"payload", "signature", "signature_key_id"},
        "installer-receipt-wire",
    )
    payload = wrapper.get("payload")
    signature_text = wrapper.get("signature")
    key_id = wrapper.get("signature_key_id")
    if (
        not isinstance(payload, dict)
        or not isinstance(signature_text, str)
        or not SIGNATURE_PATTERN.fullmatch(signature_text)
        or not isinstance(key_id, str)
        or not package.SHA256_PATTERN.fullmatch(key_id)
    ):
        fail("installer-receipt-wire-invalid")
    try:
        signature = base64.b64decode(
            signature_text + "==", altchars=b"-_", validate=True
        )
    except ValueError:
        fail("installer-receipt-signature-encoding-invalid")
    if len(signature) != 64:
        fail("installer-receipt-signature-size-invalid")
    receipt_public, _, actual_key_id = package.load_public_key(
        verified.members[RECEIPT_ANCHOR_MEMBER], "receipt-authority"
    )
    if (
        actual_key_id != key_id
        or key_id != verified.manifest["receipt_authority_key_id"]
    ):
        fail("installer-receipt-key-binding-invalid")
    payload_raw = package.canonical_json(payload)
    try:
        receipt_public.verify(
            signature, package.framed(INSTALL_RECEIPT_DOMAIN, payload_raw)
        )
    except InvalidSignature:
        fail("installer-receipt-signature-invalid")
    return payload, key_id


def verify_install(payload: dict[str, Any], verified: Any, key_id: str) -> None:
    expected = {
        "activation_canary_challenge_sha256",
        "activation_canary_receipt",
        "activation_canary_receipt_digest",
        "activation_canary_unit_sha256",
        "activation_canary_valid_until",
        "activation_canary_verified",
        "activation_canary_verified_at",
        "activation_performed",
        "activation_succeeded",
        "archive_digest",
        "authority_installed",
        "authority_profile",
        "backup_encryption_key_created",
        "backup_encryption_key_id",
        "candidate_authority_installed",
        "config_digest",
        "deactivation_performed",
        "deactivation_succeeded",
        "disposition",
        "envelope_sha",
        "host_machine_id_digest",
        "installed_at",
        "installer_binary_sha256",
        "installer_source_manifest_digest",
        "package_authority_key_id",
        "plan_digest",
        "previous_state_restored",
        "prior_authority_restored",
        "production_ready",
        "production_release_performed",
        "reactivation_performed",
        "reactivation_succeeded",
        "receipt_authority_key_id",
        "recovery_performed",
        "recovery_succeeded",
        "release_generation",
        "rollback_performed",
        "rollback_succeeded",
        "runtime_sha",
        "schema",
        "systemd_socket_active",
        "upgraded_existing_authority",
        "version",
        "workflow_sha",
    }
    exact_keys(payload, expected, "install-receipt-payload")
    build = package.parse_strict_json(
        verified.members[BUILD_RECEIPT_MEMBER],
        "installed-build-receipt",
        trailing_newline=True,
    )
    manifest = verified.manifest
    if (
        payload["schema"]
        != "propertyquarry.release-control.single-host-install-receipt.v2"
        or payload["version"] != 2
        or payload["authority_profile"] != package.PROFILE
        or payload["disposition"]
        not in ("installed-and-active", "already-installed")
        or payload["archive_digest"] != verified.archive_sha256
        or payload["config_digest"] != manifest["config_digest"]
        or payload["plan_digest"] != manifest["plan_digest"]
        or payload["runtime_sha"] != manifest["runtime_sha"]
        or payload["workflow_sha"] != manifest["workflow_sha"]
        or payload["envelope_sha"] != manifest["envelope_sha"]
        or payload["host_machine_id_digest"]
        != manifest["host_machine_id_digest"]
        or payload["release_generation"] != manifest["release_generation"]
        or payload["package_authority_key_id"]
        != manifest["package_authority_key_id"]
        or payload["receipt_authority_key_id"] != key_id
        or not isinstance(payload["backup_encryption_key_id"], str)
        or not package.SHA256_PATTERN.fullmatch(
            payload["backup_encryption_key_id"]
        )
        or payload["installer_binary_sha256"]
        != build["installer_binary_sha256"]
        or payload["installer_source_manifest_digest"]
        != build["source_manifest_digest"]
        or not isinstance(payload["installed_at"], int)
        or isinstance(payload["installed_at"], bool)
        or payload["installed_at"] < 1
    ):
        fail("install-receipt-binding-invalid")
    verify_activation_canary(payload, verified, key_id)
    for field in (
        "authority_installed",
        "candidate_authority_installed",
        "activation_performed",
        "activation_succeeded",
        "systemd_socket_active",
    ):
        if payload[field] is not True:
            fail("install-receipt-success-claim-invalid")
    for field in (
        "production_ready",
        "production_release_performed",
        "rollback_performed",
        "rollback_succeeded",
    ):
        if payload[field] is not False:
            fail("install-receipt-authority-claim-invalid")
    for field in (
        "deactivation_performed",
        "deactivation_succeeded",
        "previous_state_restored",
        "prior_authority_restored",
        "reactivation_performed",
        "reactivation_succeeded",
        "recovery_performed",
        "recovery_succeeded",
        "upgraded_existing_authority",
        "backup_encryption_key_created",
    ):
        if not isinstance(payload[field], bool):
            fail("install-receipt-boolean-invalid")


def verify_activation_canary(
    outer: dict[str, Any], verified: Any, key_id: str
) -> None:
    wrapper = outer.get("activation_canary_receipt")
    if not isinstance(wrapper, dict):
        fail("activation-canary-receipt-invalid")
    exact_keys(
        wrapper,
        {"payload", "signature", "signature_key_id"},
        "activation-canary-receipt-wire",
    )
    inner = wrapper.get("payload")
    signature_text = wrapper.get("signature")
    if (
        not isinstance(inner, dict)
        or not isinstance(signature_text, str)
        or not SIGNATURE_PATTERN.fullmatch(signature_text)
        or wrapper.get("signature_key_id") != key_id
    ):
        fail("activation-canary-receipt-invalid")
    exact_keys(
        inner,
        {
            "authority_profile",
            "challenge_sha256",
            "config_digest",
            "controller_sha256",
            "github_immutable_oidc_subject_verified",
            "github_repository_runner_admin_read_verified",
            "immutable_subject",
            "package_authority_key_id",
            "package_manifest_digest",
            "plan_digest",
            "receipt_authority_key_id",
            "repository",
            "repository_id",
            "repository_owner_id",
            "runtime_sha",
            "schema",
            "unit_sha256",
            "valid_until",
            "verified_at",
            "version",
            "workflow_sha",
        },
        "activation-canary-receipt-payload",
    )
    try:
        signature = base64.b64decode(
            signature_text + "==", altchars=b"-_", validate=True
        )
    except ValueError:
        fail("activation-canary-signature-encoding-invalid")
    if len(signature) != 64:
        fail("activation-canary-signature-size-invalid")
    receipt_public, _, observed_key_id = package.load_public_key(
        verified.members[RECEIPT_ANCHOR_MEMBER], "activation-canary-authority"
    )
    if observed_key_id != key_id:
        fail("activation-canary-key-binding-invalid")
    inner_raw = package.canonical_json(inner)
    try:
        receipt_public.verify(
            signature, package.framed(ACTIVATION_CANARY_DOMAIN, inner_raw)
        )
    except InvalidSignature:
        fail("activation-canary-signature-invalid")
    wrapper_raw = package.canonical_json(wrapper)
    files = {
        item["install_path"]: item
        for item in verified.manifest["files"]
        if isinstance(item, dict) and isinstance(item.get("install_path"), str)
    }
    controller = files.get(
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-single-host-v2"
    )
    unit = files.get(
        "/usr/lib/systemd/system/"
        "propertyquarry-release-single-host-v2-activation-canary.service"
    )
    installed_at = outer.get("installed_at")
    verified_at = inner.get("verified_at")
    valid_until = inner.get("valid_until")
    manifest = verified.manifest
    if (
        outer.get("activation_canary_verified") is not True
        or not isinstance(installed_at, int)
        or isinstance(installed_at, bool)
        or not isinstance(verified_at, int)
        or isinstance(verified_at, bool)
        or not isinstance(valid_until, int)
        or isinstance(valid_until, bool)
        or not (1 <= verified_at <= (1 << 62))
        or not (1 <= valid_until <= (1 << 62))
        or valid_until - verified_at != 120
        or not (verified_at <= installed_at <= valid_until)
        or not isinstance(controller, dict)
        or not isinstance(unit, dict)
        or inner.get("schema")
        != "propertyquarry.release-control.single-host-activation-canary-receipt.v2"
        or inner.get("version") != 2
        or inner.get("authority_profile") != package.PROFILE
        or inner.get("challenge_sha256")
        != outer.get("activation_canary_challenge_sha256")
        or inner.get("unit_sha256")
        != outer.get("activation_canary_unit_sha256")
        or verified_at != outer.get("activation_canary_verified_at")
        or valid_until != outer.get("activation_canary_valid_until")
        or package.sha256(wrapper_raw)
        != outer.get("activation_canary_receipt_digest")
        or inner.get("config_digest") != manifest["config_digest"]
        or inner.get("plan_digest") != manifest["plan_digest"]
        or inner.get("runtime_sha") != manifest["runtime_sha"]
        or inner.get("workflow_sha") != manifest["workflow_sha"]
        or inner.get("package_authority_key_id")
        != manifest["package_authority_key_id"]
        or inner.get("receipt_authority_key_id") != key_id
        or inner.get("package_manifest_digest")
        != package.sha256(verified.members["manifest.v2.json"])
        or inner.get("controller_sha256") != controller.get("sha256")
        or inner.get("unit_sha256") != unit.get("sha256")
        or inner.get("repository") != package.REPOSITORY
        or inner.get("repository_id") != package.REPOSITORY_ID
        or inner.get("repository_owner_id") != package.REPOSITORY_OWNER_ID
        or inner.get("immutable_subject")
        != (
            "repo:ArchonMegalon@11421547/propertyquarry@1257593732:"
            "environment:propertyquarry-production"
        )
        or inner.get("github_repository_runner_admin_read_verified") is not True
        or inner.get("github_immutable_oidc_subject_verified") is not True
        or not isinstance(inner.get("challenge_sha256"), str)
        or not package.SHA256_PATTERN.fullmatch(inner["challenge_sha256"])
    ):
        fail("activation-canary-binding-invalid")


def verify_runner(payload: dict[str, Any], verified: Any, key_id: str) -> None:
    expected = {
        "archive_bytes",
        "archive_sha256",
        "authority_manifest_digest",
        "authority_profile",
        "disposition",
        "installed_at",
        "installed_path",
        "package_authority_key_id",
        "production_ready",
        "receipt_authority_key_id",
        "runner_archive_installed",
        "runner_registered",
        "schema",
        "version",
    }
    exact_keys(payload, expected, "runner-receipt-payload")
    if (
        payload["schema"]
        != "propertyquarry.release-control.single-host-runner-install-receipt.v2"
        or payload["version"] != 2
        or payload["authority_profile"] != package.PROFILE
        or payload["disposition"] not in ("installed", "already-installed")
        or payload["archive_bytes"] != 225628509
        or payload["archive_sha256"]
        != "sha256:4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf"
        or payload["authority_manifest_digest"]
        != package.sha256(verified.members["manifest.v2.json"])
        or payload["installed_path"]
        != "/usr/lib/propertyquarry-release-runner-v2/actions-runner-linux-x64-2.335.1.tar.gz"
        or payload["package_authority_key_id"]
        != verified.manifest["package_authority_key_id"]
        or payload["receipt_authority_key_id"] != key_id
        or payload["runner_archive_installed"] is not True
        or payload["runner_registered"] is not False
        or payload["production_ready"] is not False
        or not isinstance(payload["installed_at"], int)
        or isinstance(payload["installed_at"], bool)
        or payload["installed_at"] < 1
    ):
        fail("runner-receipt-binding-invalid")


def verify_credential(
    payload: dict[str, Any],
    verified: Any,
    key_id: str,
    expected_credential_instance_sha256: str,
) -> None:
    expected = {
        "archive_digest",
        "authority_profile",
        "credential_ciphertext_bytes",
        "credential_ciphertext_sha256",
        "credential_gid",
        "credential_instance_sha256",
        "credential_install_performed",
        "credential_mode",
        "credential_path",
        "credential_present",
        "credential_source_transport",
        "credential_uid",
        "disposition",
        "host_mutation_performed",
        "installed_at",
        "package_authority_key_id",
        "plaintext_digest_recorded",
        "production_ready",
        "receipt_authority_key_id",
        "recovery_performed",
        "release_generation",
        "rotation_performed",
        "round_trip_verified",
        "runtime_sha",
        "schema",
        "systemd_credential_key",
        "systemd_credential_name",
        "token_material_recorded",
        "version",
        "workflow_sha",
    }
    exact_keys(payload, expected, "credential-receipt-payload")
    manifest = verified.manifest
    disposition = payload.get("disposition")
    expected_state = {
        "provisioned": (True, False),
        "recovered-and-provisioned": (True, True),
        "already-provisioned": (False, False),
    }.get(disposition)
    if (
        payload.get("schema")
        != "propertyquarry.release-control.single-host-github-credential-receipt.v2"
        or payload.get("version") != 2
        or payload.get("authority_profile") != package.PROFILE
        or expected_state is None
        or payload.get("archive_digest") != verified.archive_sha256
        or payload.get("runtime_sha") != manifest["runtime_sha"]
        or payload.get("workflow_sha") != manifest["workflow_sha"]
        or payload.get("release_generation") != manifest["release_generation"]
        or payload.get("package_authority_key_id")
        != manifest["package_authority_key_id"]
        or payload.get("receipt_authority_key_id") != key_id
        or payload.get("credential_path")
        != "/etc/propertyquarry-release-single-host-v2/github-api-token.cred"
        or payload.get("credential_mode") != "0400"
        or payload.get("credential_uid") != 0
        or payload.get("credential_gid") != 0
        or payload.get("credential_source_transport") != "named-fifo-fd8"
        or payload.get("credential_instance_sha256")
        != expected_credential_instance_sha256
        or not package.SHA256_PATTERN.fullmatch(
            expected_credential_instance_sha256
        )
        or payload.get("systemd_credential_name") != "github-api-token"
        or payload.get("systemd_credential_key") != "host"
        or not isinstance(payload.get("credential_ciphertext_sha256"), str)
        or not package.SHA256_PATTERN.fullmatch(
            payload["credential_ciphertext_sha256"]
        )
        or not isinstance(payload.get("credential_ciphertext_bytes"), int)
        or isinstance(payload.get("credential_ciphertext_bytes"), bool)
        or not 64 <= payload["credential_ciphertext_bytes"] <= 64 * 1024
        or not isinstance(payload.get("installed_at"), int)
        or isinstance(payload.get("installed_at"), bool)
        or payload["installed_at"] < 1
        or payload.get("credential_present") is not True
        or payload.get("round_trip_verified") is not True
        or payload.get("rotation_performed") is not False
        or payload.get("token_material_recorded") is not False
        or payload.get("plaintext_digest_recorded") is not True
        or payload.get("production_ready") is not False
        or payload.get("credential_install_performed") is not expected_state[0]
        or payload.get("host_mutation_performed") is not expected_state[0]
        or payload.get("recovery_performed") is not expected_state[1]
    ):
        fail("credential-receipt-binding-invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--kind", choices=("credential", "install", "runner"), required=True
    )
    parser.add_argument("--package", required=True)
    parser.add_argument("--package-authority-public-key", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--expected-credential-instance-sha256")
    arguments = parser.parse_args()
    if arguments.kind == "credential":
        if (
            not isinstance(
                arguments.expected_credential_instance_sha256, str
            )
            or not package.SHA256_PATTERN.fullmatch(
                arguments.expected_credential_instance_sha256
            )
        ):
            fail("credential-instance-binding-invalid")
    elif arguments.expected_credential_instance_sha256 is not None:
        fail("credential-instance-binding-invalid")
    verified = package.verify_package(
        arguments.package, arguments.package_authority_public_key
    )
    raw = package.read_regular(
        arguments.receipt,
        package.MAX_JSON_BYTES,
        expected_modes=(0o600,),
    )
    payload, key_id = verify_wire(raw, verified)
    if arguments.kind == "install":
        verify_install(payload, verified, key_id)
    elif arguments.kind == "runner":
        verify_runner(payload, verified, key_id)
    else:
        verify_credential(
            payload,
            verified,
            key_id,
            arguments.expected_credential_instance_sha256,
        )
    result = {
        "authoritative": False,
        "kind": arguments.kind,
        "production_ready": False,
        "receipt_sha256": package.sha256(raw),
        "schema": "propertyquarry.release-control.single-host-receipt-verification-result.v2",
        "signature_verified": True,
        "version": 2,
    }
    sys.stdout.buffer.write(package.canonical_json(result) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except package.PackageFailure as error:
        sys.stderr.write(f"propertyquarry-receipt-rejected:{error}\n")
        raise SystemExit(50)
