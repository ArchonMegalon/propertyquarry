from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "tools" / "provision-github-credential-with-docker.sh"
BROKER = ROOT / "tools" / "verify-github-credential-stream.py"
HELPER = ROOT / "internal" / "installhelper" / "credential.go"


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

    def test_token_uses_only_caller_fd8_and_private_installer_fifo(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        for token in (
            'credential_token_fd="${PROPERTYQUARRY_GITHUB_CREDENTIAL_TOKEN_FD:-}"',
            '[[ "$credential_token_fd" == "8" ]]',
            "GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN",
            'mkfifo -m 0600 -- "$credential_fifo" "$gate_fifo" "$status_fifo"',
            "--token-fd 8 --gate-fd 7 --status-fd 9",
            "exec 8<&-",
            "dst=/input/github-api-token.pipe,readonly",
            'provision-github-credential "$credential_instance_sha256"',
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--cap-add SYS_CHROOT",
            "--security-opt no-new-privileges",
            "--ulimit core=0:0",
            "--kind credential",
            '--expected-credential-instance-sha256 "$credential_instance_sha256"',
        ):
            self.assertIn(token, text)
        for forbidden in (
            "/home/tibor/.config/gh",
            "hosts.yml",
            "gh auth",
            "gh api",
            "-e GH_TOKEN",
            "--env GH_TOKEN",
            "-e GITHUB_TOKEN",
            "--env GITHUB_TOKEN",
            "$(gh auth token)",
            "`gh auth token`",
            "github_pat_",
        ):
            self.assertNotIn(forbidden, text)

    def test_broker_is_gated_until_package_and_image_verification(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        broker = text.index('/usr/bin/python3 -I -B "$credential_broker"')
        close_caller = text.index("exec 8<&-", broker)
        verify = text.index(
            '"$image_verifier" verify-image "$helper_image_id" '
            '"$package_path" "$package_anchor_path"',
            close_caller,
        )
        gate = text.index("printf '%s\\n' 'verify' >&7", verify)
        verified_status = text.index(
            "credential-instance-sha256=sha256:",
            gate,
        )
        mutation = text.index("docker run --rm --pull never")
        self.assertLess(broker, close_caller)
        self.assertLess(close_caller, verify)
        self.assertLess(verify, gate)
        self.assertLess(gate, verified_status)
        self.assertLess(verified_status, mutation)

    def test_receipt_is_bound_to_verified_instance_before_success(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        docker = text.index("docker run --rm --pull never")
        receipt = text.index(
            "propertyquarry-release-single-host-v2-github-credential-receipt.json",
            docker,
        )
        verifier = text.index('"$receipt_verifier" --kind credential', receipt)
        expected = text.index(
            '--expected-credential-instance-sha256 '
            '"$credential_instance_sha256"',
            verifier,
        )
        output = text.index("printf '%s\\n' \"$receipt\"", verifier)
        self.assertLess(docker, receipt)
        self.assertLess(receipt, verifier)
        self.assertLess(verifier, expected)
        self.assertLess(expected, output)

    def test_broker_disables_dumps_and_does_not_use_proxy_or_redirects(self) -> None:
        text = BROKER.read_text(encoding="utf-8")
        for token in (
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))",
            "prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)",
            "http.client.HTTPSConnection(",
            'API_HOST = "api.github.com"',
            'API_VERSION = "2026-03-10"',
            "TOKEN_PATTERN = re.compile(rb\"^github_pat_",
            "write_token_fifo(",
        ):
            self.assertIn(token, text)
        for forbidden in (
            "urllib.request",
            "requests.",
            "os.environ[",
            "subprocess.",
            "follow_redirect",
        ):
            self.assertNotIn(forbidden, text)

    def test_root_helper_rechecks_instance_and_dump_controls_before_mutation(
        self,
    ) -> None:
        text = HELPER.read_text(encoding="utf-8")
        dump_call = text.index("disableCredentialDumps()")
        fifo_read = text.index("readGitHubTokenFIFO(", dump_call)
        instance_check = text.index(
            "expectedCredentialInstanceSHA256",
            fifo_read,
        )
        transform = text.index(
            'runHostSystemdCredentialTransform(FixedHostRoot, "encrypt"',
            instance_check,
        )
        self.assertLess(dump_call, fifo_read)
        self.assertLess(fifo_read, instance_check)
        self.assertLess(instance_check, transform)
        for token in (
            "syscall.Setrlimit(syscall.RLIMIT_CORE",
            "syscall.SYS_PRCTL",
            '"credential_instance_sha256":',
            '"plaintext_digest_recorded":    true',
            '"token_material_recorded":      false',
        ):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
