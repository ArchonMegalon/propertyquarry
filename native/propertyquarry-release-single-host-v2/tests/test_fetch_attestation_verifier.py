from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = MODULE_ROOT / "tools" / "fetch-attestation-verifier.sh"
SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_attestation_fetch_materializer_tests",
    MODULE_ROOT / "tools" / "materialize.py",
)
assert SPEC is not None and SPEC.loader is not None
materialize = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = materialize
SPEC.loader.exec_module(materialize)


class FetchAttestationVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="propertyquarry-attestation-fetch-test-"
        )
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [os.fspath(SCRIPT), *arguments],
            cwd=MODULE_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )

    def test_argument_parent_and_no_replace_gates_fail_before_network(self) -> None:
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o755)
        os.chmod(unsafe, 0o755)
        existing = self.root / "existing"
        existing.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        cases = (
            (),
            ("relative-output",),
            (os.fspath(existing),),
            (os.fspath(unsafe / "output"),),
            (os.fspath(link / "output"),),
            (os.fspath(self.root / "contains space"),),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                completed = self._run(*arguments)
                self.assertEqual(completed.returncode, 50, completed)
                self.assertEqual(
                    completed.stderr,
                    b"propertyquarry-attestation-verifier-download-rejected\n",
                )
        self.assertFalse(any(item.name.startswith(".gh-attestation-verifier.") for item in self.root.iterdir()))

    def test_supply_chain_pins_and_atomic_publication_are_literal(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        required = (
            "set -euo pipefail",
            "--proto '=https' --proto-redir '=https'",
            "--noproxy '*'",
            "https://github.com/cli/cli/releases/download/v2.96.0/gh_2.96.0_linux_amd64.tar.gz",
            "1:14652560",
            "83d5c2ccad5498f58bf6368acb1ab32588cf43ab3a4b1c301bf36328b1c8bd60",
            "1:40722594",
            "56b8bbbb27b066ecb33dbef9a256dc9d1314adaeff0908a752feba6c34053b40",
            "3c2cc7f357dc064ec527fdcd78da6e9245c21a381e1abaa0f2b62b186bcac1a1",
            "gh version 2.96.0 (2026-07-02)",
            "renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)",
            "os.fsync(directory)",
        )
        for literal in required:
            self.assertIn(literal, source)
        for variable in (
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "LD_PRELOAD",
        ):
            self.assertIn(variable, source)

    @unittest.skipUnless(
        os.environ.get("PROPERTYQUARRY_RUN_LIVE_ATTESTATION_FETCH_TEST") == "1",
        "set PROPERTYQUARRY_RUN_LIVE_ATTESTATION_FETCH_TEST=1 for pinned live fetch",
    )
    def test_live_pinned_download_is_accepted_by_materializer_loader(self) -> None:
        output = self.root / "verifier"
        completed = subprocess.run(
            [os.fspath(SCRIPT), os.fspath(output)],
            cwd=MODULE_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=660,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, os.fsencode(output) + b"\n")
        binary, trusted_root = materialize._load_attestation_verifier(
            os.fspath(output)
        )
        self.assertEqual(binary, output / "gh")
        self.assertEqual(trusted_root, output / "trusted_root.jsonl")
        self.assertFalse(
            any(
                item.name.startswith(".gh-attestation-verifier.")
                for item in self.root.iterdir()
            )
        )


if __name__ == "__main__":
    unittest.main()
