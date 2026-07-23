from __future__ import annotations

import importlib.util
import io
import os
import struct
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_tour_package_test_module",
    MODULE_ROOT / "tools" / "tour_package.py",
)
assert SPEC is not None and SPEC.loader is not None
tour_package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tour_package
SPEC.loader.exec_module(tour_package)


class TourPackageTests(unittest.TestCase):
    NOW = 2_000_000_000

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-tour-package-test-"
        )
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.package_private = Ed25519PrivateKey.generate()
        self.receipt_private = Ed25519PrivateKey.generate()
        self.package_anchor = self._public_pem(self.package_private)
        self.receipt_anchor = self._public_pem(self.receipt_private)
        self.receipt_key = self._private_pem(self.receipt_private)
        _, _, self.package_key_id = tour_package.package.load_public_key(
            self.package_anchor, "fixture-package-anchor"
        )
        _, _, self.receipt_key_id = tour_package.package.load_public_key(
            self.receipt_anchor, "fixture-receipt-anchor"
        )
        self.authority_root = self.root / "authority"
        self.authority_root.mkdir(mode=0o700)
        os.chmod(self.authority_root, 0o700)
        self._write_authority()
        self.machine_id = self._write(
            "machine-id", b"0123456789abcdef0123456789abcdef\n", 0o444
        )
        self.controller = self._write(
            "propertyquarry-release-single-host-v2",
            self._synthetic_static_elf(),
            0o755,
        )
        self.build_receipt = self._write(
            "build-receipt.v2.json",
            self._build_receipt_raw(),
            0o644,
        )
        self.original_authority_root = (
            tour_package.materialize.PRODUCTION_RECEIPT_AUTHORITY_ROOT
        )
        self.original_load_authority = tour_package.materialize._load_authority
        self.original_machine_id = tour_package.MACHINE_ID_PATH
        tour_package.materialize.PRODUCTION_RECEIPT_AUTHORITY_ROOT = (
            self.authority_root
        )
        tour_package.materialize._load_authority = self._load_authority
        tour_package.MACHINE_ID_PATH = self.machine_id

    def tearDown(self) -> None:
        tour_package.materialize.PRODUCTION_RECEIPT_AUTHORITY_ROOT = (
            self.original_authority_root
        )
        tour_package.materialize._load_authority = self.original_load_authority
        tour_package.MACHINE_ID_PATH = self.original_machine_id
        self.temporary.cleanup()

    @staticmethod
    def _public_pem(private: Ed25519PrivateKey) -> bytes:
        return private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @staticmethod
    def _private_pem(private: Ed25519PrivateKey) -> bytes:
        return private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def _write(self, relative: str, raw: bytes, mode: int) -> Path:
        path = self.root / relative
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            mode,
        )
        try:
            written = 0
            while written < len(raw):
                written += os.write(descriptor, raw[written:])
        finally:
            os.close(descriptor)
        os.chmod(path, mode)
        return path

    def _write_private_authority_file(self, name: str, raw: bytes) -> None:
        path = self.authority_root / name
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o400,
        )
        try:
            os.write(descriptor, raw)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o400)

    def _write_authority(self) -> None:
        bootstrap = {
            "created_at_epoch": self.NOW - 100,
            "package_authority_key_id": self.package_key_id,
            "package_authority_private_sha256": tour_package.package.sha256(
                self._private_pem(self.package_private)
            ),
            "package_authority_public_sha256": tour_package.package.sha256(
                self.package_anchor
            ),
            "package_authority_source": "/fixture/canonical-authority",
            "receipt_authority_key_id": self.receipt_key_id,
            "receipt_authority_public_sha256": tour_package.package.sha256(
                self.receipt_anchor
            ),
            "schema": tour_package.materialize.AUTHORITY_SCHEMA,
            "version": 2,
        }
        bootstrap_raw = tour_package.package.canonical_json(bootstrap)
        bootstrap_signature = self.package_private.sign(
            tour_package.package.framed(
                tour_package.materialize.AUTHORITY_SIGNATURE_DOMAIN,
                bootstrap_raw,
            )
        )
        for name, raw in {
            "authority-bootstrap.v2.json": bootstrap_raw,
            "authority-bootstrap.v2.sig": bootstrap_signature,
            "receipt-authority-v2.key": self.receipt_key,
            "receipt-authority-v2.pem": self.receipt_anchor,
        }.items():
            self._write_private_authority_file(name, raw)

    def _load_authority(self, root: str):
        self.assertEqual(Path(root), self.authority_root)
        return (
            self.package_anchor,
            self.package_private,
            self.package_private.public_key(),
            self.receipt_private,
            self.receipt_private.public_key(),
            self.package_key_id,
            self.receipt_key_id,
        )

    @staticmethod
    def _synthetic_static_elf() -> bytes:
        ident = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
        header = struct.pack(
            "<HHIQQQIHHHHHH",
            2,
            62,
            1,
            0x400000,
            64,
            0,
            0,
            64,
            56,
            2,
            0,
            0,
            0,
        )
        load = struct.pack(
            "<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, 176, 176, 4096
        )
        stack = struct.pack(
            "<IIQQQQQQ", 0x6474E551, 6, 0, 0, 0, 0, 0, 16
        )
        return ident + header + load + stack

    def _build_receipt_raw(self) -> bytes:
        controller = self._synthetic_static_elf()
        receipt = {
            "authoritative": False,
            "binary_mode": "0755",
            "binary_sha256": tour_package.package.sha256(controller),
            "binary_size": len(controller),
            "build_flags": [
                "-mod=readonly",
                "-trimpath",
                "-buildvcs=false",
                "-buildmode=exe",
            ],
            "go_tests_passed_in_both_builds": True,
            "host_network_namespace_isolated": False,
            "independent_toolchain_extractions": True,
            "installer_binary_mode": "0555",
            "installer_binary_sha256": "sha256:" + "9" * 64,
            "installer_binary_size": 4096,
            "installer_package_authority_bound": True,
            "installer_package_authority_key_id": self.package_key_id,
            "module_network_resolution_disabled": True,
            "package_signature_verified": False,
            "performs_release_effects": False,
            "production_ready": False,
            "receipt_published_last": True,
            "reproducible_double_build": True,
            "root_install_performed": False,
            "schema": tour_package.package.BUILD_RECEIPT_SCHEMA,
            "scratch_execution_contract": "linux-amd64-static-et-exec-v1",
            "source_manifest_digest": "sha256:" + "8" * 64,
            "static_elf_verified_in_both_builds": True,
            "toolchain": "go1.26.5 linux/amd64",
            "toolchain_archive_bytes": 66879095,
            "toolchain_archive_sha256": (
                "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
            ),
            "version": 2,
        }
        return tour_package.package.canonical_json(receipt) + b"\n"

    def _materialize_and_build(self, suffix: str = "") -> tuple[Path, Path]:
        materialization_root = self.root / f"materialization{suffix}"
        tour_package.materialize_tour(
            controller_path=os.fspath(self.controller),
            build_receipt_path=os.fspath(self.build_receipt),
            output=os.fspath(materialization_root),
            now=self.NOW,
        )
        archive = self.root / f"tour-package{suffix}.tar"
        tour_package.build_package(
            controller_path=os.fspath(self.controller),
            build_receipt_path=os.fspath(self.build_receipt),
            materialization_root=os.fspath(materialization_root),
            output=os.fspath(archive),
            now=self.NOW,
        )
        return materialization_root, archive

    @staticmethod
    def _extract_archive(
        path: Path,
    ) -> tuple[dict[str, bytes], dict[str, int]]:
        members: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        with tarfile.open(path, mode="r:") as archive:
            for info in archive.getmembers():
                extracted = archive.extractfile(info)
                assert extracted is not None
                members[info.name] = extracted.read()
                modes[info.name] = info.mode
        return members, modes

    def _write_repacked(
        self,
        name: str,
        members: dict[str, bytes],
        modes: dict[str, int],
    ) -> Path:
        raw = tour_package.package._tar_bytes(
            {path: (modes[path], value) for path, value in members.items()}
        )
        return self._write(name, raw, 0o400)

    def test_build_is_deterministic_exact_and_has_no_runtime_authority(self) -> None:
        materialization_root, first = self._materialize_and_build("-one")
        _, second = self._materialize_and_build("-two")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(stat_mode(first), 0o400)
        verified = tour_package.verify_package(
            os.fspath(first), now=self.NOW
        )
        self.assertEqual(set(verified.members), tour_package.EXACT_MEMBER_NAMES)
        self.assertEqual(set(verified.manifest), tour_package.MANIFEST_KEYS)
        self.assertEqual(
            set(verified.materialization), tour_package.MATERIALIZATION_KEYS
        )
        self.assertEqual(
            verified.materialization["allowed_operations"],
            tour_package.ALLOWED_OPERATIONS,
        )
        self.assertEqual(
            verified.materialization["valid_until_epoch"] - self.NOW,
            tour_package.MATERIALIZATION_TTL_SECONDS,
        )
        self.assertTrue(
            verified.materialization["publication_dispatch_authorized"]
        )
        for key in (
            "authoritative",
            "host_install_permitted",
            "network_required",
            "performs_release_effects",
            "persistent_credential_installation_permitted",
            "production_ready",
            "runtime_deployment_permitted",
        ):
            self.assertFalse(verified.materialization[key])
        for key in (
            "host_install_permitted",
            "network_required",
            "package_signing_private_key_included",
            "performs_runtime_deployment",
            "runtime_deployment_permitted",
        ):
            self.assertFalse(verified.manifest[key])
        joined_names = "\n".join(sorted(verified.members))
        for forbidden in (
            "authority.v2.json",
            "transaction-plan",
            "runtime-deploy",
            "systemd",
            "github",
        ):
            self.assertNotIn(forbidden, joined_names)
        self.assertEqual(
            sorted(item["path"] for item in verified.manifest["files"]),
            sorted(tour_package.FILE_LAYOUT),
        )
        for path, (mode, purpose) in tour_package.FILE_LAYOUT.items():
            record = next(
                item
                for item in verified.manifest["files"]
                if item["path"] == path
            )
            self.assertEqual(record["mode"], f"{mode:04o}")
            self.assertEqual(record["purpose"], purpose)
        for child in materialization_root.iterdir():
            self.assertEqual(stat_mode(child), 0o400)

    def test_payload_tamper_and_host_rebinding_are_rejected(self) -> None:
        _, archive = self._materialize_and_build()
        members, modes = self._extract_archive(archive)
        controller = bytearray(members[tour_package.CONTROLLER_PATH])
        controller[-1] ^= 1
        members[tour_package.CONTROLLER_PATH] = bytes(controller)
        tampered = self._write_repacked("tampered-controller.tar", members, modes)
        with self.assertRaisesRegex(
            tour_package.package.PackageFailure,
            "tour-manifest-file-binding-invalid",
        ):
            tour_package.verify_package(os.fspath(tampered), now=self.NOW)
        os.chmod(self.machine_id, 0o644)
        with self.machine_id.open("wb") as stream:
            stream.write(b"fedcba9876543210fedcba9876543210\n")
        os.chmod(self.machine_id, 0o444)
        with self.assertRaisesRegex(
            tour_package.package.PackageFailure,
            "tour-materialization-binding-invalid",
        ):
            tour_package.verify_package(os.fspath(archive), now=self.NOW)

    def test_full_runtime_manifest_signature_domain_is_rejected(self) -> None:
        _, archive = self._materialize_and_build()
        members, modes = self._extract_archive(archive)
        members[tour_package.MANIFEST_SIGNATURE_NAME] = (
            self.package_private.sign(
                tour_package.package.framed(
                    tour_package.package.MANIFEST_SIGNATURE_DOMAIN,
                    members[tour_package.MANIFEST_NAME],
                )
            )
        )
        crossed = self._write_repacked(
            "cross-runtime-manifest-domain.tar", members, modes
        )
        with self.assertRaisesRegex(
            tour_package.package.PackageFailure,
            "tour-manifest-signature-invalid",
        ):
            tour_package.verify_package(os.fspath(crossed), now=self.NOW)

    def test_full_runtime_materialization_signature_domain_is_rejected(
        self,
    ) -> None:
        _, archive = self._materialize_and_build()
        members, modes = self._extract_archive(archive)
        materialization_raw = members[tour_package.MATERIALIZATION_PATH]
        members[tour_package.MATERIALIZATION_SIGNATURE_PATH] = (
            self.package_private.sign(
                tour_package.package.framed(
                    tour_package.package.MATERIALIZATION_SIGNATURE_DOMAIN,
                    materialization_raw,
                )
            )
        )
        manifest = tour_package.package.parse_strict_json(
            members[tour_package.MANIFEST_NAME], "fixture-tour-manifest"
        )
        for record in manifest["files"]:
            if record["path"] == tour_package.MATERIALIZATION_SIGNATURE_PATH:
                record["sha256"] = tour_package.package.sha256(
                    members[tour_package.MATERIALIZATION_SIGNATURE_PATH]
                )
                record["size"] = len(
                    members[tour_package.MATERIALIZATION_SIGNATURE_PATH]
                )
        manifest_raw = tour_package.package.canonical_json(manifest)
        members[tour_package.MANIFEST_NAME] = manifest_raw
        members[tour_package.MANIFEST_SIGNATURE_NAME] = (
            self.package_private.sign(
                tour_package.package.framed(
                    tour_package.PACKAGE_SIGNATURE_DOMAIN, manifest_raw
                )
            )
        )
        crossed = self._write_repacked(
            "cross-runtime-materialization-domain.tar", members, modes
        )
        with self.assertRaisesRegex(
            tour_package.package.PackageFailure,
            "tour-materialization-signature-invalid",
        ):
            tour_package.verify_package(os.fspath(crossed), now=self.NOW)

    def test_expired_materialization_is_rejected(self) -> None:
        _, archive = self._materialize_and_build()
        expired_now = (
            self.NOW + tour_package.MATERIALIZATION_TTL_SECONDS + 1
        )
        with self.assertRaisesRegex(
            tour_package.package.PackageFailure,
            "tour-materialization-shape-or-freshness-invalid",
        ):
            tour_package.verify_package(
                os.fspath(archive),
                now=expired_now,
            )
        recovery = tour_package.verify_package(
            os.fspath(archive),
            require_fresh=False,
            now=expired_now,
        )
        self.assertFalse(recovery.fresh)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
