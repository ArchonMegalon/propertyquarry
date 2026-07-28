from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from scripts import propertyquarry_release_local_identity as identity


CANDIDATE_SHA = "b" * 40
WORKFLOW_SHA = "a" * 40


def _bootstrap(
    tmp_path: Path,
    *,
    name: str = "authority",
    phase_hook=None,
) -> identity.BootstrapResult:
    tmp_path.chmod(0o700)
    return identity.bootstrap_local_identity(
        state_root=str(tmp_path / name),
        candidate_sha=CANDIDATE_SHA,
        workflow_sha=WORKFLOW_SHA,
        phase_hook=phase_hook,
    )


def _replace_package_key_triple(root: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    replacements = {
        root / "keys" / "package-authority-v2.key": private_raw,
        root / "anchors" / "package-authority-v2.pem": public_raw,
        root
        / "package-input"
        / "trust.d"
        / "package-authority-v2.pem": public_raw,
    }
    for path, raw in replacements.items():
        path.write_bytes(raw)
        path.chmod(0o600)


def _mutate_controller_with_valid_canonical_json(root: Path) -> None:
    path = root / "package-input" / "controller-v2.json"
    document = json.loads(path.read_bytes())
    document["limits"]["callback_timeout_seconds"] += 1
    path.write_bytes(identity._canonical_bytes(document))
    path.chmod(0o600)


def _relative_tree(root: Path) -> set[str]:
    result: set[str] = set()
    for directory, directories, files in os.walk(root):
        base = Path(directory)
        result.update(
            str((base / name).relative_to(root)) + "/" for name in directories
        )
        result.update(str((base / name).relative_to(root)) for name in files)
    return result


def test_bootstrap_creates_six_distinct_keys_and_exact_public_bundle(
    tmp_path: Path,
) -> None:
    result = _bootstrap(tmp_path)
    root = Path(result.state_root)
    verified = identity.verify_bootstrap_receipt(state_root=str(root))

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    expected_directories, expected_files = identity._expected_state_paths()
    assert _relative_tree(root) == {
        *(path + "/" for path in expected_directories),
        *expected_files,
    }
    package_input = root / "package-input"
    actual_package_files = {
        str(path.relative_to(package_input))
        for path in package_input.rglob("*")
        if path.is_file()
    }
    assert actual_package_files == set(identity.PACKAGE_INPUT_FILES)
    assert len(actual_package_files) == 9

    key_ids: set[str] = set()
    for role, stem in identity.KEY_SPECS:
        private_path = root / "keys" / f"{stem}.key"
        anchor_path = root / "anchors" / f"{stem}.pem"
        package_path = package_input / "trust.d" / f"{stem}.pem"
        assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(anchor_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(package_path.stat().st_mode) == 0o600
        assert private_path.stat().st_nlink == 1
        assert anchor_path.stat().st_nlink == 1
        assert package_path.stat().st_nlink == 1

        private_key = serialization.load_pem_private_key(
            private_path.read_bytes(), password=None
        )
        public_key = serialization.load_pem_public_key(anchor_path.read_bytes())
        assert isinstance(private_key, Ed25519PrivateKey)
        assert isinstance(public_key, Ed25519PublicKey)
        assert package_path.read_bytes() == anchor_path.read_bytes()
        assert private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) == public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_id = identity._digest(
            public_key.public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        assert key_id not in key_ids, role
        key_ids.add(key_id)

    receipt = verified["receipt"]
    assert receipt["schema"] == identity.BOOTSTRAP_SCHEMA
    assert receipt["private_key_count"] == 6
    assert receipt["public_key_count"] == 6
    assert receipt["package_input_file_count"] == 9
    assert receipt["authority_scope"] == {
        "kind": "local-docker",
        "scope_id": "propertyquarry-local-docker",
        "authoritative_for_identity_bootstrap": True,
        "external_production_authority": False,
        "public_launch_authority": False,
        "performs_release_effects": False,
    }
    assert verified["receipt_sha256"] == result.receipt_sha256
    assert verified["receipt_signature_sha256"] == (
        result.receipt_signature_sha256
    )
    assert result.package_key_id in key_ids
    records = {record["role"]: record for record in receipt["keys"]}
    package_digests = receipt["package_input_file_sha256"]
    assert set(package_digests) == set(identity.PACKAGE_INPUT_FILES)
    for role, stem in identity.KEY_SPECS:
        record = records[role]
        private_raw = (root / "keys" / f"{stem}.key").read_bytes()
        anchor_raw = (root / "anchors" / f"{stem}.pem").read_bytes()
        package_relative = f"trust.d/{stem}.pem"
        package_raw = (package_input / package_relative).read_bytes()
        assert record["private_key_sha256"] == identity._digest(private_raw)
        assert record["external_anchor_sha256"] == identity._digest(anchor_raw)
        assert record["package_input_sha256"] == identity._digest(package_raw)
        assert package_digests[package_relative] == identity._digest(package_raw)


def test_bootstrap_public_outputs_and_receipt_never_leak_private_material(
    tmp_path: Path,
) -> None:
    result = _bootstrap(tmp_path)
    root = Path(result.state_root)
    public_paths = [
        *(root / "anchors").iterdir(),
        *(root / "package-input").rglob("*"),
        root / "identity-bootstrap-receipt.v2.json",
        root / "identity-bootstrap-receipt.v2.sig",
    ]
    public_bytes = b"".join(
        path.read_bytes() for path in public_paths if path.is_file()
    )
    assert b"PRIVATE KEY" not in public_bytes
    assert b"BEGIN PRIVATE" not in public_bytes

    receipt = json.loads(
        (root / "identity-bootstrap-receipt.v2.json").read_bytes()
    )
    serialized = json.dumps(receipt, sort_keys=True)
    assert "private_key_path" in serialized
    assert "private_key_bytes" not in serialized
    assert "private_key_pem" not in serialized


def test_bootstrap_is_tofu_noreplace_and_partial_state_fails_closed(
    tmp_path: Path,
) -> None:
    first = _bootstrap(tmp_path)
    before = {
        path: path.read_bytes()
        for path in Path(first.state_root).rglob("*")
        if path.is_file()
    }
    with pytest.raises(identity.LocalIdentityError, match="state-exists"):
        _bootstrap(tmp_path)
    after = {
        path: path.read_bytes()
        for path in Path(first.state_root).rglob("*")
        if path.is_file()
    }
    assert after == before

    partial = tmp_path / "partial"
    partial.mkdir(mode=0o700)
    (partial / "attacker-marker").write_text("preserve", encoding="ascii")
    with pytest.raises(identity.LocalIdentityError, match="state-exists"):
        _bootstrap(tmp_path, name="partial")
    assert (partial / "attacker-marker").read_text(encoding="ascii") == "preserve"


def test_publish_collision_injected_after_generation_never_replaces_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "authority"

    def collide(phase: str) -> None:
        if phase == "before-publish":
            target.mkdir(mode=0o700)
            (target / "owner-data").write_text("preserve", encoding="ascii")

    with pytest.raises(identity.LocalIdentityError, match="state-exists"):
        _bootstrap(tmp_path, phase_hook=collide)
    assert (target / "owner-data").read_text(encoding="ascii") == "preserve"
    assert not list(tmp_path.glob(".authority.initializing.*"))


def test_coordinated_package_key_substitution_before_publish_fails_closed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "authority"

    def substitute(phase: str) -> None:
        if phase != "before-publish":
            return
        [temporary] = list(tmp_path.glob(".authority.initializing.*"))
        _replace_package_key_triple(temporary)

    with pytest.raises(
        identity.LocalIdentityError,
        match="state-(key|package-input|pinned|receipt)",
    ):
        _bootstrap(tmp_path, phase_hook=substitute)

    assert not target.exists()
    assert not list(tmp_path.glob(".authority.initializing.*"))
    assert not list(tmp_path.glob(".authority.rollback.*"))


def test_valid_canonical_config_mutation_before_publish_fails_closed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "authority"

    def mutate(phase: str) -> None:
        if phase != "before-publish":
            return
        [temporary] = list(tmp_path.glob(".authority.initializing.*"))
        _mutate_controller_with_valid_canonical_json(temporary)

    with pytest.raises(
        identity.LocalIdentityError,
        match="state-(package-input|package-config|pinned|receipt)",
    ):
        _bootstrap(tmp_path, phase_hook=mutate)

    assert not target.exists()
    assert not list(tmp_path.glob(".authority.initializing.*"))
    assert not list(tmp_path.glob(".authority.rollback.*"))


def test_postrename_config_mutation_rolls_back_exact_state_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "authority"
    rename_noreplace = identity._rename_noreplace
    publish_mutations = 0

    def publish_then_mutate(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal publish_mutations
        rename_noreplace(parent_fd, source, destination)
        if destination == target.name:
            publish_mutations += 1
            _mutate_controller_with_valid_canonical_json(target)

    monkeypatch.setattr(identity, "_rename_noreplace", publish_then_mutate)
    with pytest.raises(
        identity.LocalIdentityError,
        match="state-(package-input|package-config|pinned|receipt)",
    ):
        _bootstrap(tmp_path)

    assert publish_mutations == 1
    assert not target.exists()
    assert not list(tmp_path.glob(".authority.initializing.*"))
    assert not list(tmp_path.glob(".authority.rollback.*"))


def test_verify_rejects_coordinated_package_key_and_config_substitution(
    tmp_path: Path,
) -> None:
    first = _bootstrap(tmp_path, name="key-substitution")
    first_root = Path(first.state_root)
    _replace_package_key_triple(first_root)
    with pytest.raises(
        identity.LocalIdentityError,
        match="state-(key|package-input|receipt)",
    ):
        identity.verify_bootstrap_receipt(state_root=str(first_root))

    second = _bootstrap(tmp_path, name="config-substitution")
    second_root = Path(second.state_root)
    _mutate_controller_with_valid_canonical_json(second_root)
    with pytest.raises(
        identity.LocalIdentityError,
        match="state-(package-input|package-config|receipt)",
    ):
        identity.verify_bootstrap_receipt(state_root=str(second_root))


def test_temp_root_path_replacement_is_detected_before_publish(
    tmp_path: Path,
) -> None:
    moved: Path | None = None

    def replace(phase: str) -> None:
        nonlocal moved
        if phase != "before-publish":
            return
        [temporary] = list(tmp_path.glob(".authority.initializing.*"))
        moved = temporary.with_name("detached-original")
        temporary.rename(moved)
        temporary.mkdir(mode=0o700)
        (temporary / "attacker-marker").write_text("attack", encoding="ascii")

    with pytest.raises(
        identity.LocalIdentityError,
        match="temporary-root-replaced|temporary-cleanup-target-replaced",
    ):
        _bootstrap(tmp_path, phase_hook=replace)
    assert not (tmp_path / "authority").exists()
    assert moved is not None and moved.exists()


def test_unsafe_or_symlinked_parent_is_rejected_before_key_generation(
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(
        identity.LocalIdentityError, match="state-parent-metadata-unsafe"
    ):
        identity.bootstrap_local_identity(
            state_root=str(unsafe / "authority"),
            candidate_sha=CANDIDATE_SHA,
            workflow_sha=WORKFLOW_SHA,
        )
    assert list(unsafe.iterdir()) == []

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(
        identity.LocalIdentityError, match="state-parent-(type-invalid|symlink-rejected)"
    ):
        identity.bootstrap_local_identity(
            state_root=str(alias / "authority"),
            candidate_sha=CANDIDATE_SHA,
            workflow_sha=WORKFLOW_SHA,
        )
    assert list(real.iterdir()) == []


def test_receipt_signature_and_state_metadata_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    result = _bootstrap(tmp_path)
    root = Path(result.state_root)
    signature = root / "identity-bootstrap-receipt.v2.sig"
    raw = bytearray(signature.read_bytes())
    raw[0] ^= 1
    signature.write_bytes(raw)
    signature.chmod(0o600)
    with pytest.raises(identity.LocalIdentityError, match="receipt-signature-invalid"):
        identity.verify_bootstrap_receipt(state_root=str(root))

    signature.chmod(0o666)
    with pytest.raises(identity.LocalIdentityError, match="state-file-metadata-invalid"):
        identity.verify_bootstrap_receipt(state_root=str(root))


@pytest.mark.parametrize(
    ("candidate", "workflow", "code"),
    [
        ("B" * 40, WORKFLOW_SHA, "candidate-sha-invalid"),
        ("b" * 39, WORKFLOW_SHA, "candidate-sha-invalid"),
        (CANDIDATE_SHA, True, "workflow-sha-invalid"),
    ],
)
def test_identity_inputs_are_exact_before_any_write(
    tmp_path: Path,
    candidate: object,
    workflow: object,
    code: str,
) -> None:
    with pytest.raises(identity.LocalIdentityError, match=code):
        identity.bootstrap_local_identity(
            state_root=str(tmp_path / "authority"),
            candidate_sha=candidate,  # type: ignore[arg-type]
            workflow_sha=workflow,  # type: ignore[arg-type]
    )
    assert list(tmp_path.iterdir()) == []


def test_identity_cleanup_erases_pinned_tree_without_deleting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir(mode=0o700)
    nested = temporary / "nested"
    nested.mkdir(mode=0o700)
    (nested / "owned-data").write_text("erase me", encoding="ascii")
    parent_fd = os.open(str(tmp_path), identity._open_flags(directory=True))
    pinned_fd = os.open(str(temporary), identity._open_flags(directory=True))
    expected_identity = identity._directory_identity(os.fstat(pinned_fd))
    original_erase = identity._erase_directory_contents_fd
    detached = tmp_path / "detached-owned"
    replacement_marker = temporary / "replacement-marker"

    def replace_name_then_erase_pinned(descriptor: int) -> None:
        temporary.rename(detached)
        temporary.mkdir(mode=0o700)
        replacement_marker.write_text("preserve", encoding="ascii")
        original_erase(descriptor)

    monkeypatch.setattr(
        identity,
        "_erase_directory_contents_fd",
        replace_name_then_erase_pinned,
    )
    try:
        with pytest.raises(
            identity.LocalIdentityError,
            match="temporary-cleanup-target-replaced",
        ):
            identity._remove_temp(parent_fd, temporary.name, expected_identity)
        assert replacement_marker.read_text(encoding="ascii") == "preserve"
        assert list(detached.iterdir()) == []
        assert os.fstat(pinned_fd).st_ino == detached.stat().st_ino
        assert os.fstat(pinned_fd).st_nlink > 0
    finally:
        os.close(pinned_fd)
        os.close(parent_fd)
