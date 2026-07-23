from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_source_closure_tests",
    MODULE_ROOT / "tools" / "verify-source-closure.py",
)
assert SPEC is not None and SPEC.loader is not None
source_closure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = source_closure
SPEC.loader.exec_module(source_closure)


class SourceClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-source-closure-test-"
        )
        self.base = Path(self.temporary.name)
        self.root = self.base / "module"
        self.root.mkdir(mode=0o700)
        self.snapshot_index = 0
        self._write("go.mod", b"module example.invalid/closure\n", 0o644)
        self._write("internal/demo.go", b"package internal\n", 0o644)
        self._write("internal/demo_test.go", b"package internal\n", 0o644)
        self._write(
            "tools/source-files.txt",
            b"go.mod\ninternal/demo.go\ninternal/demo_test.go\ntools/source-files.txt\n",
            0o644,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative: str, raw: bytes, mode: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        os.chmod(path, mode)
        return path

    @property
    def manifest(self) -> Path:
        return self.root / "tools/source-files.txt"

    def _create(self) -> tuple[Path, Path]:
        self.snapshot_index += 1
        snapshot = self.base / f"snapshot-{self.snapshot_index}"
        snapshot.mkdir(mode=0o700)
        material = self.base / f"material-{self.snapshot_index}"
        source_closure.create_snapshot(
            self.root, self.manifest, snapshot, material
        )
        return snapshot, material

    def assert_failure(self, code: str, callback) -> None:
        with self.assertRaises(SystemExit) as caught:
            callback()
        self.assertEqual(str(caught.exception), code)

    def test_valid_exact_inventory_snapshot_and_go_inputs(self) -> None:
        snapshot, material = self._create()
        source_closure.verify_snapshot(snapshot, material)
        go_list = self.base / "go-list.json"
        go_list.write_text(
            json.dumps(
                {
                    "Dir": os.fspath(snapshot / "internal"),
                    "GoFiles": ["demo.go"],
                    "TestGoFiles": ["demo_test.go"],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        source_closure.verify_go_list(snapshot, material, go_list)

    def test_snapshot_and_material_writes_tolerate_partial_syscalls(self) -> None:
        original_write = source_closure.os.write

        def partial_write(descriptor: int, raw: bytes) -> int:
            return original_write(descriptor, raw[:7])

        with mock.patch.object(source_closure.os, "write", partial_write):
            snapshot, material = self._create()
        source_closure.verify_snapshot(snapshot, material)

    def test_unlisted_go_and_listed_missing_are_rejected(self) -> None:
        self._write("internal/hidden.go", b"package internal\n", 0o644)
        self.assert_failure("source-inventory-mismatch", self._create)
        (self.root / "internal/hidden.go").unlink()
        self.manifest.write_bytes(
            b"go.mod\ninternal/demo.go\ninternal/demo_test.go\ninternal/missing.go\ntools/source-files.txt\n"
        )
        self.assert_failure("source-inventory-mismatch", self._create)

    def test_duplicate_unsorted_unsafe_and_generated_entries_are_rejected(self) -> None:
        for raw, code in (
            (
                b"go.mod\ninternal/demo.go\ninternal/demo.go\ninternal/demo_test.go\ntools/source-files.txt\n",
                "source-manifest-order-invalid",
            ),
            (
                b"internal/demo.go\ngo.mod\ninternal/demo_test.go\ntools/source-files.txt\n",
                "source-manifest-order-invalid",
            ),
            (
                b"../escape\ngo.mod\ninternal/demo.go\ninternal/demo_test.go\ntools/source-files.txt\n",
                "source-manifest-path-invalid",
            ),
        ):
            with self.subTest(code=code, raw=raw):
                self.manifest.write_bytes(raw)
                self.assert_failure(code, self._create)
        self.manifest.write_bytes(
            b"go.mod\ninternal/demo.go\ninternal/demo_test.go\ntools/source-files.txt\n"
        )
        generated = self._write("tools/__pycache__/x.pyc", b"generated", 0o644)
        self.assert_failure("source-generated-artifact-forbidden", self._create)
        generated.unlink()

    def test_mode_flip_and_symlink_are_rejected(self) -> None:
        demo = self.root / "internal/demo.go"
        os.chmod(demo, 0o600)
        self.assert_failure("source-file-metadata-invalid", self._create)
        os.chmod(demo, 0o644)
        link = self.root / "internal/link.go"
        link.symlink_to(demo)
        self.assert_failure("source-symlink-forbidden", self._create)

    def test_snapshot_mutation_after_digest_is_detected(self) -> None:
        snapshot, material = self._create()
        demo = snapshot / "internal/demo.go"
        os.chmod(snapshot, 0o700)
        os.chmod(snapshot / "internal", 0o700)
        os.chmod(demo, 0o644)
        demo.write_bytes(b"package internal // mutated\n")
        os.chmod(demo, 0o444)
        os.chmod(snapshot / "internal", 0o500)
        os.chmod(snapshot, 0o500)
        self.assert_failure(
            "source-snapshot-digest-invalid",
            lambda: source_closure.verify_snapshot(snapshot, material),
        )

    def test_go_list_extra_and_ignored_go_files_are_rejected(self) -> None:
        snapshot, material = self._create()
        go_list = self.base / "go-list.json"
        go_list.write_text(
            json.dumps(
                {
                    "Dir": os.fspath(snapshot / "internal"),
                    "GoFiles": ["demo.go", "unbound.go"],
                    "TestGoFiles": ["demo_test.go"],
                }
            ),
            encoding="utf-8",
        )
        self.assert_failure(
            "source-go-input-unbound",
            lambda: source_closure.verify_go_list(snapshot, material, go_list),
        )
        go_list.write_text(
            json.dumps(
                {
                    "Dir": os.fspath(snapshot / "internal"),
                    "GoFiles": ["demo.go"],
                    "TestGoFiles": [],
                }
            ),
            encoding="utf-8",
        )
        self.assert_failure(
            "source-go-input-set-mismatch",
            lambda: source_closure.verify_go_list(snapshot, material, go_list),
        )


if __name__ == "__main__":
    unittest.main()
