from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "tools" / "provision-github-credential-with-docker.sh"


class CredentialBootstrapWrapperTests(unittest.TestCase):
    def test_no_arguments_fail_before_provider_or_docker_use(self) -> None:
        subprocess.run(["bash", "-n", os.fspath(WRAPPER)], check=True)
        result = subprocess.run(
            ["bash", os.fspath(WRAPPER)],
            env={
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "GH_TOKEN": "must-not-be-used",
                "DOCKER_HOST": "tcp://example.invalid:2375",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 50)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "propertyquarry-github-credential-provision-rejected\n",
        )

    def test_token_uses_only_private_fifo_fd8_and_fixed_installer_input(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        for token in (
            "GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN",
            "mkfifo -m 0600",
            'exec 8>"$credential_fifo"',
            "exec /usr/bin/gh auth token --hostname github.com >&8",
            "dst=/input/github-api-token.pipe,readonly",
            "provision-github-credential",
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--cap-add SYS_CHROOT",
            "--security-opt no-new-privileges",
            "--kind credential",
        ):
            self.assertIn(token, text)
        for forbidden in (
            "-e GH_TOKEN",
            "--env GH_TOKEN",
            "-e GITHUB_TOKEN",
            "--env GITHUB_TOKEN",
            "$(gh auth token)",
            "`gh auth token`",
            "github_pat_",
        ):
            self.assertNotIn(forbidden, text)

    def test_package_and_image_are_verified_before_any_token_or_host_effect(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        verify = text.index(
            '"$image_verifier" verify-image "$helper_image_id" '
            '"$package_path" "$package_anchor_path"'
        )
        permission_probe = text.index(
            "/usr/bin/gh api --method GET "
            "repos/ArchonMegalon/propertyquarry/actions/runners"
        )
        fifo = text.index("mkfifo -m 0600")
        producer = text.index("/usr/bin/gh auth token --hostname github.com")
        mutation = text.index("docker run --rm --pull never")
        self.assertLess(verify, permission_probe)
        self.assertLess(permission_probe, fifo)
        self.assertLess(fifo, producer)
        self.assertLess(producer, mutation)
        self.assertIn(
            "repos/ArchonMegalon/propertyquarry/actions/oidc/customization/sub",
            text,
        )

    def test_receipt_is_verified_and_contains_no_plaintext_contract(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        docker = text.index("docker run --rm --pull never")
        receipt = text.index(
            "propertyquarry-release-single-host-v2-github-credential-receipt.json",
            docker,
        )
        verifier = text.index('"$receipt_verifier" --kind credential', receipt)
        output = text.index("printf '%s\\n' \"$receipt\"", verifier)
        self.assertLess(docker, receipt)
        self.assertLess(receipt, verifier)
        self.assertLess(verifier, output)


if __name__ == "__main__":
    unittest.main()
