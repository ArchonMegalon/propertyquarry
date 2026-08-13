from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import struct

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import propertyquarry_release_authenticated_package as authenticated
from scripts import propertyquarry_release_local_identity as identity
from scripts import propertyquarry_release_package_payload as payload


CANDIDATE_SHA = "b" * 40
WORKFLOW_SHA = "a" * 40


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _deterministic_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = payload.TEMPLATE_ROOT
    template_root = tmp_path / "deterministic-templates"
    template_root.mkdir(mode=0o700)
    template_root.chmod(0o700)
    # Keep the golden vector independent of the checkout process's umask.
    for relative in payload.TEMPLATE_FILES:
        _write(
            template_root / relative,
            (source_root / relative).read_bytes(),
            0o644,
        )
    for directory, _directories, _files in os.walk(template_root):
        path = Path(directory)
        path.chmod(0o700 if path == template_root else 0o755)
    monkeypatch.setattr(payload, "TEMPLATE_ROOT", template_root)


def _synthetic_static_elf(label: str) -> bytes:
    payload_bytes = b"\xc3" + label.encode("ascii")
    program_offset = 64
    code_offset = program_offset + 2 * 56
    total = code_offset + len(payload_bytes)
    identity = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    return b"".join(
        (
            struct.pack(
                "<16sHHIQQQIHHHHHH",
                identity,
                2,
                62,
                1,
                0x400000 + code_offset,
                program_offset,
                0,
                0,
                64,
                56,
                2,
                0,
                0,
                0,
            ),
            struct.pack(
                "<IIQQQQQQ",
                1,
                5,
                0,
                0x400000,
                0x400000,
                total,
                total,
                0x1000,
            ),
            struct.pack("<IIQQQQQQ", 0x6474E551, 6, 0, 0, 0, 0, 0, 8),
            payload_bytes,
        )
    )


def _native_receipt(binary_data: dict[str, bytes]) -> dict[str, object]:
    source_digest = "sha256:" + "d" * 64
    return {
        "schema": "propertyquarry.release-control.native-build-receipt.v2",
        "authoritative": False,
        "production_ready": False,
        "reproducible_double_build": True,
        "distinct_absolute_source_roots": True,
        "isolated_build_caches": True,
        "independent_toolchain_extractions": True,
        "go_subprocess_environment_allowlisted": True,
        "go_subprocess_inherited_environment_cleared": True,
        "module_network_resolution_disabled": True,
        "host_network_namespace_isolated": False,
        "go_tests_passed_in_both_builds": True,
        "scratch_execution": dict(payload.NATIVE_SCRATCH_EXECUTION),
        "source_manifest_reverified_after_build": True,
        "receipt_published_last": True,
        "root_install_performed": False,
        "package_signature_verified": False,
        "builder_identity_authenticated": False,
        "toolchain": "go1.26.5 linux/amd64",
        "toolchain_archive_bytes": 66_879_095,
        "toolchain_archive_sha256": (
            "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
        ),
        "go_binary_sha256": (
            "sha256:8da5fd321795754b994c64e3eb8a5a14ff47bd285559a7e876f3c79abafc67f9"
        ),
        "source_manifest_sha256": source_digest,
        "build_flags": [
            "-mod=readonly",
            "-trimpath",
            "-buildvcs=false",
            "-buildmode=exe",
        ],
        "ldflags": (
            "-buildid= -linkmode=internal -X propertyquarry.local/"
            "release-control-v2/internal/"
            "releasecontrol.SourceManifestDigest="
            + source_digest
            + " -X propertyquarry.local/release-control-v2/internal/"
            "releasecontrol.ScratchExecutionContract="
            + payload.NATIVE_SCRATCH_EXECUTION["contract"]
        ),
        "build_environment": dict(payload._NATIVE_BUILD_ENVIRONMENT),
        "binary_mode": "0755",
        "binary_sizes": {
            name: len(binary_data[name]) for name in payload._NATIVE_NAMES
        },
        "binaries": {
            name: _sha(binary_data[name]) for name in payload._NATIVE_NAMES
        },
    }


def _synthetic_native(root: Path) -> Path:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    binary_data = {
        name: _synthetic_static_elf(name) for name in payload._NATIVE_NAMES
    }
    for name, raw in binary_data.items():
        _write(root / name, raw, 0o755)
    _write(root / "build-receipt.json", _canonical(_native_receipt(binary_data)), 0o644)
    return root


def _deterministic_authority(tmp_path: Path) -> identity.BootstrapResult:
    root = tmp_path / "deterministic-authority"
    package_input = root / "package-input"
    for directory in (
        root,
        root / "keys",
        root / "anchors",
        package_input,
        package_input / "trust.d",
    ):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)

    package_private = root / "keys" / "package-authority-v2.key"
    package_anchor = root / "anchors" / "package-authority-v2.pem"
    package_key_id = ""
    for role, stem in identity.KEY_SPECS:
        seed = (
            bytes(range(32))
            if role == "package"
            else hashlib.sha256(f"propertyquarry-vector:{role}".encode("ascii")).digest()
        )
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        private_raw = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        _write(package_input / "trust.d" / f"{stem}.pem", public_raw, 0o600)
        if role == "package":
            _write(package_private, private_raw, 0o600)
            _write(package_anchor, public_raw, 0o600)
            package_key_id = _sha(
                private_key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )

    _write(
        package_input / "controller-v2.json",
        _canonical(identity._controller_config(workflow_sha=WORKFLOW_SHA)),
        0o600,
    )
    _write(
        package_input / "watchdog-v2.json",
        _canonical(identity._watchdog_config()),
        0o600,
    )
    _write(
        package_input / "policy-v2.json",
        _canonical(
            identity._root_policy(
                candidate_sha=CANDIDATE_SHA,
                workflow_sha=WORKFLOW_SHA,
            )
        ),
        0o600,
    )
    return identity.BootstrapResult(
        state_root=str(root),
        receipt_sha256=_sha(b"deterministic-receipt"),
        receipt_signature_sha256=_sha(b"deterministic-receipt-signature"),
        package_key_id=package_key_id,
        package_private_key=str(package_private),
        package_external_anchor=str(package_anchor),
        package_input=str(package_input),
    )


def _authority(tmp_path: Path, name: str = "authority") -> identity.BootstrapResult:
    tmp_path.chmod(0o700)
    return identity.bootstrap_local_identity(
        state_root=str(tmp_path / name),
        candidate_sha=CANDIDATE_SHA,
        workflow_sha=WORKFLOW_SHA,
    )


def _unsigned_payload(
    tmp_path: Path,
    authority: identity.BootstrapResult,
    name: str = "unsigned-payload",
) -> Path:
    output = tmp_path / name
    payload.assemble_payload(
        native_bundle=str(_synthetic_native(tmp_path / f"native-{name}")),
        private_bundle=authority.package_input,
        service_gid=1999,
        output=str(output),
    )
    return output


def _wrapper(
    tmp_path: Path,
    *,
    authority: identity.BootstrapResult | None = None,
    unsigned: Path | None = None,
    name: str = "authenticated-wrapper",
    phase_hook=None,
) -> tuple[identity.BootstrapResult, Path, Path]:
    authority = authority or _authority(tmp_path)
    unsigned = unsigned or _unsigned_payload(tmp_path, authority)
    output = tmp_path / name
    authenticated.create_authenticated_wrapper(
        payload=str(unsigned),
        private_key=authority.package_private_key,
        external_anchor=authority.package_external_anchor,
        output=str(output),
        phase_hook=phase_hook,
    )
    return authority, unsigned, output


def _tree(root: Path) -> dict[str, tuple[str, int, bytes | None]]:
    result: dict[str, tuple[str, int, bytes | None]] = {}
    for directory, directories, files in os.walk(root):
        base = Path(directory)
        for name in directories:
            path = base / name
            result[str(path.relative_to(root))] = (
                "directory",
                stat.S_IMODE(path.stat().st_mode),
                None,
            )
        for name in files:
            path = base / name
            result[str(path.relative_to(root))] = (
                "file",
                stat.S_IMODE(path.stat().st_mode),
                path.read_bytes(),
            )
    return result


def _role_file(wrapper: Path, role: str = "controller-executable") -> Path:
    from scripts import propertyquarry_release_installation_model as installation

    return wrapper / "payload" / "rootfs" / installation.ROLE_BY_NAME[role].path[1:]


def test_bootstrap_package_input_is_accepted_unchanged_by_real_assembler(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    package_input = Path(authority.package_input)
    original = _tree(package_input)

    unsigned = _unsigned_payload(tmp_path, authority)

    assert _tree(package_input) == original
    manifest = json.loads(
        (unsigned / "installation-manifest.v2.json").read_text("ascii")
    )
    assert len(manifest["roles"]) == 19


def test_wrapper_authenticates_exact_unchanged_payload_with_external_anchor(
    tmp_path: Path,
) -> None:
    authority, unsigned, wrapper = _wrapper(tmp_path)
    original = _tree(unsigned)
    result = authenticated.verify_wrapper(
        wrapper=str(wrapper),
        external_anchor=authority.package_external_anchor,
    )

    assert _tree(wrapper / "payload") == original
    assert _tree(unsigned) == original
    assert set(path.name for path in wrapper.iterdir()) == {
        "payload",
        "authentication.v2.json",
        "authentication.v2.sig",
    }
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700
    assert stat.S_IMODE((wrapper / "payload").stat().st_mode) == 0o700
    assert (wrapper / "authentication.v2.sig").stat().st_size == 64
    document = result["authentication"]
    assert document["schema"] == authenticated.AUTHENTICATION_SCHEMA
    assert document["version"] == 2
    assert document["signature_profile"] == {
        "algorithm": "ed25519",
        "encoding": "raw-64-byte",
        "key_id": authority.package_key_id,
        "signed_message": (
            "domain-separated-uint64be-length-prefixed-canonical-json"
        ),
    }
    assert document["authority_scope"] == {
        "kind": "local-docker",
        "scope_id": "propertyquarry-local-docker",
        "authoritative_for_package_authentication": True,
        "external_production_authority": False,
        "public_launch_authority": False,
        "performs_release_effects": False,
    }
    assert document["payload"]["file_count"] == 21
    assert document["payload"]["role_count"] == 19
    assert result["authentication_sha256"] == _sha(
        (wrapper / "authentication.v2.json").read_bytes()
    )
    assert result["signature_sha256"] == _sha(
        (wrapper / "authentication.v2.sig").read_bytes()
    )
    assert b"PRIVATE KEY" not in b"".join(
        path.read_bytes() for path in wrapper.rglob("*") if path.is_file()
    )


def test_wrapper_signature_is_deterministic_for_same_key_and_payload(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    unsigned = _unsigned_payload(tmp_path, authority)
    _, _, first = _wrapper(
        tmp_path,
        authority=authority,
        unsigned=unsigned,
        name="wrapper-one",
    )
    _, _, second = _wrapper(
        tmp_path,
        authority=authority,
        unsigned=unsigned,
        name="wrapper-two",
    )
    assert (first / "authentication.v2.json").read_bytes() == (
        second / "authentication.v2.json"
    ).read_bytes()
    assert (first / "authentication.v2.sig").read_bytes() == (
        second / "authentication.v2.sig"
    ).read_bytes()
    assert _tree(first / "payload") == _tree(second / "payload")


def test_fixed_seed_cross_language_authentication_vector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deterministic_templates(tmp_path, monkeypatch)
    authority = _deterministic_authority(tmp_path)
    unsigned = _unsigned_payload(tmp_path, authority, name="vector-payload")
    _, _, wrapper = _wrapper(
        tmp_path,
        authority=authority,
        unsigned=unsigned,
        name="vector-wrapper",
    )
    snapshot = authenticated._snapshot_payload(str(unsigned))
    tree_raw = _canonical(
        {
            "schema": authenticated.TREE_SCHEMA,
            "entries": authenticated._tree_entries(snapshot),
        }
    )
    authentication_raw = (wrapper / authenticated.AUTHENTICATION_FILE).read_bytes()
    signature = (wrapper / authenticated.SIGNATURE_FILE).read_bytes()
    expected_document = {
        "schema": authenticated.AUTHENTICATION_SCHEMA,
        "version": 2,
        "signature_profile": {
            "algorithm": "ed25519",
            "encoding": "raw-64-byte",
            "key_id": (
                "sha256:a050837d85070582ccf7394b0988847c"
                "c312cb88259b894899f6f239cf1791a5"
            ),
            "signed_message": (
                "domain-separated-uint64be-length-prefixed-canonical-json"
            ),
        },
        "authority_scope": {
            "kind": "local-docker",
            "scope_id": "propertyquarry-local-docker",
            "authoritative_for_package_authentication": True,
            "external_production_authority": False,
            "public_launch_authority": False,
            "performs_release_effects": False,
        },
        "payload": {
            "tree_digest": (
                "sha256:052ddacda77fd074fb206f1093a183a9"
                "7f48f339652c17d9b6ebc75b4724afb2"
            ),
            "file_count": 21,
            "directory_count": 15,
            "role_count": 19,
            "installation_manifest_sha256": (
                "sha256:e09f2055d6560c3238f9c9a50b475d3b"
                "a3a826f5428679a4f634df97046d72c5"
            ),
            "package_payload_receipt_sha256": (
                "sha256:4d103e699c44c958169e3df9525b1b17"
                "3c3e0e003ae2cd7c703e216ca0831932"
            ),
            "native_build_receipt_sha256": (
                "sha256:cdd3ce09ab91ae315138d3ab516de5ceb"
                "6cc6c7ec0cc13e1f6e99dd564d16300"
            ),
        },
    }

    assert bytes(range(32)).hex() == (
        "000102030405060708090a0b0c0d0e0f"
        "101112131415161718191a1b1c1d1e1f"
    )
    assert authority.package_key_id == expected_document["signature_profile"][
        "key_id"
    ]
    assert len(authenticated._tree_entries(snapshot)) == 36
    assert _sha(tree_raw) == (
        "sha256:2bc127f526ac50e304ca7932f8f9ed08"
        "ed64fd2da7c16b3a08a99617547e5b8a"
    )
    assert authenticated._tree_digest(snapshot) == expected_document["payload"][
        "tree_digest"
    ]
    assert authentication_raw == _canonical(expected_document)
    assert _sha(authentication_raw) == (
        "sha256:02052cccbd0ed68decdf6299c06dc1424"
        "0a88aadbf0d0dd2d8511b6dfc15a46b"
    )
    assert _sha(
        authenticated._framed(
            authenticated.AUTHENTICATION_DOMAIN,
            authentication_raw,
        )
    ) == (
        "sha256:300eea2d4102c92358a2b52df60804ab"
        "a72426ef6880a07bb526dd1b09d6b314"
    )
    assert signature.hex() == (
        "eb4387d5c34e682ad9fcce6328e63ae3"
        "0f88a7f792d258e5d2c64fd607307ebf"
        "824e677294966432099afff68822e4a8"
        "2b0fa14a5ee6ba6800276e9d316baa0b"
    )
    assert _sha(signature) == (
        "sha256:b45a39888caa059a4f18c9bd518b50c9"
        "cd5e50e3b727feec103234dd8d2380a7"
    )


def test_external_anchor_substitution_and_private_key_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    authority, _unsigned, wrapper = _wrapper(tmp_path)
    attacker = _authority(tmp_path, "attacker-authority")
    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="payload-package-anchor-mismatch|authentication-contract-invalid",
    ):
        authenticated.verify_wrapper(
            wrapper=str(wrapper),
            external_anchor=attacker.package_external_anchor,
        )

    output = tmp_path / "mismatched-output"
    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="private-key-anchor-mismatch",
    ):
        authenticated.create_authenticated_wrapper(
            payload=str(wrapper / "payload"),
            private_key=attacker.package_private_key,
            external_anchor=authority.package_external_anchor,
            output=str(output),
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "role-byte",
        "role-mode",
        "extra-file",
        "symlink",
        "hardlink",
        "fifo",
        "authentication-byte",
        "signature-byte",
    ],
)
def test_wrapper_rejects_payload_metadata_tree_and_signature_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    authority, _unsigned, wrapper = _wrapper(tmp_path)
    role = _role_file(wrapper)
    if mutation == "role-byte":
        raw = bytearray(role.read_bytes())
        raw[-1] ^= 1
        role.write_bytes(raw)
        role.chmod(0o755)
    elif mutation == "role-mode":
        role.chmod(0o700)
    elif mutation == "extra-file":
        extra = wrapper / "payload" / "attacker-extra"
        extra.write_bytes(b"attack")
        extra.chmod(0o600)
    elif mutation == "symlink":
        role.unlink()
        role.symlink_to("/dev/null")
    elif mutation == "hardlink":
        other = _role_file(wrapper, "supervisor-executable")
        role.unlink()
        os.link(other, role)
    elif mutation == "fifo":
        role.unlink()
        os.mkfifo(role, 0o600)
    elif mutation == "authentication-byte":
        target = wrapper / "authentication.v2.json"
        raw = bytearray(target.read_bytes())
        raw[-2] ^= 1
        target.write_bytes(raw)
        target.chmod(0o644)
    else:
        target = wrapper / "authentication.v2.sig"
        raw = bytearray(target.read_bytes())
        raw[0] ^= 1
        target.write_bytes(raw)
        target.chmod(0o644)

    with pytest.raises(authenticated.AuthenticatedPackageError):
        authenticated.verify_wrapper(
            wrapper=str(wrapper),
            external_anchor=authority.package_external_anchor,
        )


def test_payload_contained_package_key_cannot_replace_external_trust(
    tmp_path: Path,
) -> None:
    authority, _unsigned, wrapper = _wrapper(tmp_path)
    attacker = _authority(tmp_path, "attacker-authority")
    package_key = _role_file(wrapper, "package-trust-root")
    package_key.write_bytes(Path(attacker.package_external_anchor).read_bytes())
    package_key.chmod(0o640)
    with pytest.raises(authenticated.AuthenticatedPackageError):
        authenticated.verify_wrapper(
            wrapper=str(wrapper),
            external_anchor=authority.package_external_anchor,
        )


def test_existing_wrapper_output_is_never_replaced(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    unsigned = _unsigned_payload(tmp_path, authority)
    output = tmp_path / "wrapper"
    output.mkdir(mode=0o700)
    marker = output / "owner-data"
    marker.write_text("preserve", encoding="ascii")
    with pytest.raises(authenticated.AuthenticatedPackageError, match="output-exists"):
        authenticated.create_authenticated_wrapper(
            payload=str(unsigned),
            private_key=authority.package_private_key,
            external_anchor=authority.package_external_anchor,
            output=str(output),
        )
    assert marker.read_text(encoding="ascii") == "preserve"


def test_payload_mutation_after_copy_aborts_without_publishing(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    unsigned = _unsigned_payload(tmp_path, authority)
    output = tmp_path / "wrapper"
    target = unsigned / "rootfs" / "usr" / "libexec" / (
        "propertyquarry-release-control/propertyquarry-release-controller-v2"
    )

    def mutate(phase: str) -> None:
        if phase == "payload-copied":
            raw = bytearray(target.read_bytes())
            raw[-1] ^= 1
            target.write_bytes(raw)
            target.chmod(0o755)

    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="payload-input-mutated|payload:input-concurrent-mutation",
    ):
        authenticated.create_authenticated_wrapper(
            payload=str(unsigned),
            private_key=authority.package_private_key,
            external_anchor=authority.package_external_anchor,
            output=str(output),
            phase_hook=mutate,
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".wrapper.assembling.*"))


def test_staged_wrapper_mutation_in_last_hook_window_never_publishes(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    unsigned = _unsigned_payload(tmp_path, authority)
    output = tmp_path / "wrapper"

    def mutate_staged_wrapper(phase: str) -> None:
        if phase != "before-publish":
            return
        staged = list(tmp_path.glob(".wrapper.assembling.*"))
        assert len(staged) == 1
        target = _role_file(staged[0])
        raw = bytearray(target.read_bytes())
        raw[-1] ^= 1
        target.write_bytes(raw)
        target.chmod(0o755)

    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="wrapper-pinned-(binding-invalid|identity-mutated)",
    ):
        authenticated.create_authenticated_wrapper(
            payload=str(unsigned),
            private_key=authority.package_private_key,
            external_anchor=authority.package_external_anchor,
            output=str(output),
            phase_hook=mutate_staged_wrapper,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".wrapper.assembling.*"))
    assert not list(tmp_path.glob(".wrapper.rollback.*"))


def test_mutation_immediately_after_rename_rolls_back_exact_published_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(tmp_path)
    unsigned = _unsigned_payload(tmp_path, authority)
    output = tmp_path / "wrapper"
    rename_noreplace = authenticated.payload_model._rename_noreplace
    publish_mutations = 0

    def publish_then_mutate(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal publish_mutations
        rename_noreplace(parent_fd, source, destination)
        if destination == output.name:
            publish_mutations += 1
            target = _role_file(output)
            raw = bytearray(target.read_bytes())
            raw[-1] ^= 1
            target.write_bytes(raw)
            target.chmod(0o755)

    monkeypatch.setattr(
        authenticated.payload_model,
        "_rename_noreplace",
        publish_then_mutate,
    )
    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="wrapper-(descriptor|pinned)",
    ):
        authenticated.create_authenticated_wrapper(
            payload=str(unsigned),
            private_key=authority.package_private_key,
            external_anchor=authority.package_external_anchor,
            output=str(output),
        )

    assert publish_mutations == 1
    assert not output.exists()
    assert not list(tmp_path.glob(".wrapper.assembling.*"))
    assert not list(tmp_path.glob(".wrapper.rollback.*"))


def test_external_anchor_symlink_hardlink_and_unsafe_mode_are_rejected(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    unsigned = _unsigned_payload(tmp_path, authority)
    original = Path(authority.package_external_anchor)

    symlink = tmp_path / "anchor-symlink.pem"
    symlink.symlink_to(original)
    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="external-anchor-(type-invalid|symlink-rejected)",
    ):
        authenticated.create_authenticated_wrapper(
            payload=str(unsigned),
            private_key=authority.package_private_key,
            external_anchor=str(symlink),
            output=str(tmp_path / "symlink-output"),
        )

    hardlink = tmp_path / "anchor-hardlink.pem"
    os.link(original, hardlink)
    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="external-anchor-metadata-invalid",
    ):
        authenticated.create_authenticated_wrapper(
            payload=str(unsigned),
            private_key=authority.package_private_key,
            external_anchor=str(hardlink),
            output=str(tmp_path / "hardlink-output"),
        )

    hardlink.unlink()
    original.chmod(0o666)
    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="external-anchor-metadata-invalid",
    ):
        authenticated.create_authenticated_wrapper(
            payload=str(unsigned),
            private_key=authority.package_private_key,
            external_anchor=str(original),
            output=str(tmp_path / "mode-output"),
        )


def test_noncanonical_authentication_and_signature_shape_fail_closed(
    tmp_path: Path,
) -> None:
    authority, _unsigned, wrapper = _wrapper(tmp_path)
    authentication_path = wrapper / "authentication.v2.json"
    document = json.loads(authentication_path.read_bytes())
    authentication_path.write_bytes(json.dumps(document, indent=2).encode("ascii"))
    authentication_path.chmod(0o644)
    with pytest.raises(
        authenticated.AuthenticatedPackageError,
        match="authentication-json-invalid|wrapper:input-concurrent-mutation",
    ):
        authenticated.verify_wrapper(
            wrapper=str(wrapper),
            external_anchor=authority.package_external_anchor,
        )

    # Rebuild and truncate the detached raw signature.
    _, _, second = _wrapper(
        tmp_path,
        authority=authority,
        unsigned=wrapper / "payload",
        name="second-wrapper",
    )
    signature = second / "authentication.v2.sig"
    signature.write_bytes(signature.read_bytes()[:-1])
    signature.chmod(0o644)
    with pytest.raises(authenticated.AuthenticatedPackageError):
        authenticated.verify_wrapper(
            wrapper=str(second),
            external_anchor=authority.package_external_anchor,
        )


def test_signature_domain_and_length_framing_are_mandatory(tmp_path: Path) -> None:
    authority, _unsigned, wrapper = _wrapper(tmp_path)
    authentication_raw = (wrapper / "authentication.v2.json").read_bytes()
    signature = (wrapper / "authentication.v2.sig").read_bytes()
    private_key = serialization.load_pem_private_key(
        Path(authority.package_private_key).read_bytes(), password=None
    )
    assert isinstance(private_key, Ed25519PrivateKey)
    assert signature == private_key.sign(
        authenticated.AUTHENTICATION_DOMAIN
        + len(authentication_raw).to_bytes(8, "big")
        + authentication_raw
    )
    assert signature != private_key.sign(authentication_raw)
    assert signature != private_key.sign(
        authenticated.AUTHENTICATION_DOMAIN + authentication_raw
    )
