#!/usr/bin/env python3
"""Static and fail-before-effect checks for the privileged Docker wrappers."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
INSTALL = TOOLS / "install-with-docker.sh"
RUNNER = TOOLS / "install-runner-with-docker.sh"
BUILD = TOOLS / "build-installer-image.sh"
EPHEMERAL = TOOLS / "run-ephemeral-runner.sh"
LIFECYCLE = TOOLS / "run-ephemeral-runner-with-docker.sh"
SCRIPTS = (INSTALL, RUNNER, BUILD, EPHEMERAL, LIFECYCLE)
DOCKER_SCRIPTS = (INSTALL, RUNNER, BUILD, LIFECYCLE)


class DockerWrapperTests(unittest.TestCase):
    def texts(self) -> dict[str, str]:
        return {path.name: path.read_text(encoding="utf-8") for path in SCRIPTS}

    def test_shell_syntax_and_no_argument_paths_fail_before_docker(self) -> None:
        subprocess.run(["bash", "-n", *(os.fspath(path) for path in SCRIPTS)], check=True)
        expected = {
            INSTALL: "propertyquarry-docker-root-install-rejected\n",
            RUNNER: "propertyquarry-docker-runner-install-rejected\n",
            BUILD: "propertyquarry-installer-image-build-rejected\n",
            EPHEMERAL: "propertyquarry-ephemeral-runner-rejected\n",
            LIFECYCLE: "propertyquarry-docker-ephemeral-runner-rejected\n",
        }
        for path, error in expected.items():
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["bash", os.fspath(path)],
                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DOCKER_HOST": "tcp://example.invalid:2375"},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(result.returncode, 50)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, error)

    def test_every_wrapper_forces_and_proves_the_local_daemon(self) -> None:
        required = (
            "export DOCKER_HOST=unix:///var/run/docker.sock",
            "-S /var/run/docker.sock",
            "docker context show",
            "docker context inspect default",
            "unix:///var/run/docker.sock",
            "docker version --format '{{.Server.Os}}:{{.Server.Arch}}'",
            '"linux:amd64"',
        )
        for path in DOCKER_SCRIPTS:
            name = path.name
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=name):
                for token in required:
                    self.assertIn(token, text)
                self.assertLess(text.index("unset BASH_ENV"), text.index("export DOCKER_HOST="))
                self.assertNotIn("tcp://", text)
                self.assertNotIn("ssh://", text)

    def test_ephemeral_runner_is_two_phase_and_prestart_has_no_host_authority(self) -> None:
        wrapper = LIFECYCLE.read_text(encoding="utf-8")
        launcher = EPHEMERAL.read_text(encoding="utf-8")
        configure_run = wrapper.index("docker run --rm --pull never -i")
        start_read = wrapper.index("authorization_marker", configure_run)
        signed_verify = wrapper.index('"$controller" runner-start-verify', start_read)
        listener_run = wrapper.index("docker run --rm --pull never ", signed_verify)
        configure_block = wrapper[configure_run:start_read]
        listener_block = wrapper[listener_run:]
        self.assertNotIn("/var/run/docker.sock,dst=/var/run/docker.sock", configure_block)
        self.assertNotIn("src=${authority_runtime}", configure_block)
        self.assertNotIn("src=${authority_config}", configure_block)
        self.assertNotIn("--group-add 112", configure_block)
        self.assertIn("src=${authority_runtime},dst=${authority_runtime},readonly", listener_block)
        self.assertIn("src=${authority_config},dst=${authority_config},readonly", listener_block)
        self.assertIn("src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly=false", listener_block)
        self.assertIn("--group-add 112", listener_block)
        self.assertLess(configure_run, start_read)
        self.assertLess(start_read, signed_verify)
        self.assertLess(signed_verify, listener_run)
        for token in (
            "assert mounts==expected",
            "assert '/var/run/docker.sock' not in mounts",
            "authority_runtime not in mounts and authority_config not in mounts",
            "session_device",
            "session_inode",
            "session_tree_sha256",
            '"$controller" runner-session-verify',
        ):
            self.assertIn(token, wrapper + launcher)
        self.assertIn("[[ ! -e /var/run/docker.sock ]]", launcher)
        self.assertIn("[[ ! -e /run/propertyquarry-release-single-host-v2 ]]", launcher)
        self.assertIn("[[ ! -e /etc/propertyquarry-release-single-host-v2 ]]", launcher)
        self.assertNotIn("config.sh", launcher)
        self.assertIn("./bin/Runner.Listener configure", launcher)
        self.assertIn("--no-default-labels", launcher)

    def test_runner_tokens_are_fd_only_and_never_docker_arguments_or_environment(self) -> None:
        wrapper = LIFECYCLE.read_text(encoding="utf-8")
        launcher = EPHEMERAL.read_text(encoding="utf-8")
        self.assertIn('PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8', wrapper)
        self.assertIn('"$controller" runner-supervise 8<&8', wrapper)
        self.assertIn("exec 8<&-", wrapper)
        self.assertIn('IFS= read -r -t 300 registration_token <&"$supervisor_output_fd"', wrapper)
        self.assertIn("unset registration_token", wrapper)
        self.assertIn("IFS= read -r registration_token <&8", launcher)
        self.assertIn("exec 8<&-", launcher)
        self.assertNotIn("--token", launcher)
        for forbidden in (
            "-e ACTIONS_RUNNER_INPUT_TOKEN",
            "--env ACTIONS_RUNNER_INPUT_TOKEN",
            "-e PROPERTYQUARRY_RUNNER_ADMIN_TOKEN",
            "--env PROPERTYQUARRY_RUNNER_ADMIN_TOKEN",
        ):
            self.assertNotIn(forbidden, wrapper)

    def test_install_stages_with_external_anchor_before_any_run(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        stage = text.index('python3 "$package_tool" stage')
        anchor = text.index('--package-authority-public-key "$package_anchor_path"')
        bound = text.index('receipt["installer_package_authority_bound"] is True')
        rootfs = text.index("verify_installer_rootfs", bound)
        run = text.index("docker run --rm --pull never")
        receipt_verify = text.index('"$receipt_verifier" --kind install')
        self.assertLess(stage, anchor)
        self.assertLess(anchor, bound)
        self.assertLess(bound, rootfs)
        self.assertLess(rootfs, run)
        self.assertLess(run, receipt_verify)
        for token in (
            'docker image save --output "$image_archive" "$helper_image_id"',
            'assert hashlib.sha256(config_raw).hexdigest() == expected_hex',
            'assert len(infos) == 1',
            'assert info.name == "propertyquarry-release-single-host-installer-v2"',
            'assert "sha256:" + hashlib.sha256(binary).hexdigest() == expected_digest',
            '"$extracted_binary" --self-test',
            '--entrypoint /propertyquarry-release-single-host-installer-v2',
        ):
            self.assertIn(token, text)

    def test_inspect_verifier_accepts_only_exact_scratch_defaults(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        marker = (
            '/usr/bin/python3 - "$inspect_path" "$helper_image_id" '
            '"$expected_size" <<\'PY\' || fail\n'
        )
        start = text.index(marker) + len(marker)
        verifier = text[start : text.index("\nPY\n", start)]
        image_id = "sha256:" + ("1" * 64)
        default_path = (
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        value = [
            {
                "Architecture": "amd64",
                "Config": {
                    "Entrypoint": [
                        "/propertyquarry-release-single-host-installer-v2"
                    ],
                    "Env": [default_path],
                    "WorkingDir": "/",
                },
                "Id": image_id,
                "Os": "linux",
                "RootFS": {
                    "Layers": ["sha256:" + ("2" * 64)],
                    "Type": "layers",
                },
                "Size": 7946289,
            }
        ]

        with tempfile.TemporaryDirectory(prefix="pq-inspect-verifier-test-") as directory:
            root = Path(directory)
            verifier_path = root / "verifier.py"
            verifier_path.write_text(verifier, encoding="utf-8")
            cases = (
                ("exact-defaults", "/", [default_path], 0),
                ("legacy-defaults", "", None, 0),
                ("unexpected-working-directory", "/tmp", [default_path], 1),
                ("unexpected-environment", "/", [default_path, "EXTRA=1"], 1),
                ("normalized-directory-legacy-environment", "/", None, 1),
                ("legacy-directory-normalized-environment", "", [default_path], 1),
            )
            for name, working_directory, environment, expected_return in cases:
                with self.subTest(case=name):
                    value[0]["Config"]["WorkingDir"] = working_directory
                    value[0]["Config"]["Env"] = environment
                    inspect_path = root / f"{name}.json"
                    inspect_path.write_text(json.dumps(value), encoding="utf-8")
                    result = subprocess.run(
                        [
                            "python3",
                            os.fspath(verifier_path),
                            os.fspath(inspect_path),
                            image_id,
                            "7946289",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected_return)

    def test_host_systemd_canary_precedes_install_in_identical_apparmor_envelope(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        canary = text.index('"$helper_image_id" host-systemd-canary')
        receipt = text.index("value['residue_present'] is False", canary)
        empty = text.index('[[ -z "$(ls -A -- "$receipt_directory")" ]]', receipt)
        install = text.index('"$helper_image_id" install || fail', empty)
        self.assertLess(canary, receipt)
        self.assertLess(receipt, empty)
        self.assertLess(empty, install)
        prefix = text[:canary]
        canary_run = prefix[prefix.rindex("docker run --rm --pull never") :]
        install_run = text[text.rindex("docker run --rm --pull never", 0, install) : install]
        for token in (
            "--cap-drop ALL",
            "--cap-add SYS_CHROOT",
            "--security-opt no-new-privileges",
            "--security-opt apparmor=unconfined",
            "--network none --pid host --read-only --user 0:0",
            "src=/,dst=/host,readonly=false,bind-propagation=rslave",
        ):
            self.assertIn(token, canary_run)
            self.assertIn(token, install_run)

    def test_network_none_installer_reaches_live_probe_only_through_host_oneshot(self) -> None:
        wrapper = INSTALL.read_text(encoding="utf-8")
        install_go = (ROOT / "internal/installhelper/install.go").read_text(
            encoding="utf-8"
        )
        unit = (
            ROOT
            / "packaging/templates/"
            "propertyquarry-release-single-host-v2-activation-canary.service"
        ).read_text(encoding="utf-8")
        self.assertIn("--network none --pid host", wrapper)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("PrivateNetwork=no", unit)
        unit_lines = unit.splitlines()
        self.assertIn(
            "ExecStart=/usr/libexec/propertyquarry-release-control/"
            "propertyquarry-release-single-host-v2 activation-probe",
            unit_lines,
        )
        self.assertIn(
            "LoadCredentialEncrypted=github-api-token:"
            "/etc/propertyquarry-release-single-host-v2/github-api-token.cred",
            unit_lines,
        )
        self.assertIn(
            "LoadCredential=activation-challenge:"
            "/run/propertyquarry-release-single-host-v2/activation-canary/"
            "activation-challenge.v2",
            unit_lines,
        )
        self.assertIn(
            "LoadCredential=receipt-authority-key:"
            "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key",
            unit_lines,
        )
        self.assertIn("StandardOutput=null", unit_lines)
        self.assertIn("LimitCORE=0", unit_lines)
        self.assertFalse(any(line.startswith("Environment=") for line in unit_lines))
        exec_start = next(line for line in unit_lines if line.startswith("ExecStart="))
        self.assertNotIn("github-api-token", exec_start)
        self.assertNotIn("activation-challenge", exec_start)
        self.assertNotIn("receipt-authority-key", exec_start)
        self.assertNotIn("RemainAfterExit=yes", unit)
        host = install_go[install_go.index("func HostSystemdOperation(") :]
        start = host.index(
            '{argv: []string{"/usr/bin/systemctl", "start", canaryUnit}}'
        )
        proof = host.index("authority.VerifyActivationCanaryReceipt", start)
        enable = host.index(
            '{argv: []string{"/usr/bin/systemctl", "enable", "--now", socketUnit}}',
            proof,
        )
        self.assertLess(start, proof)
        self.assertLess(proof, enable)

    def test_runner_reuses_package_bound_image_verification(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        verification = text.index('"$image_verifier" verify-image')
        run = text.index("docker run --rm --pull never")
        receipt_verify = text.index('"$receipt_verifier" --kind runner')
        self.assertLess(verification, run)
        self.assertLess(run, receipt_verify)
        self.assertIn('package_anchor_path="$4"', text)
        self.assertIn('--entrypoint /propertyquarry-release-single-host-installer-v2', text)

    def test_embedded_rootfs_verifier_accepts_only_the_single_bound_file(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        marker = (
            '/usr/bin/python3 - "$image_archive" "$helper_image_id" "$expected_digest" '
            '"$expected_size" "$extracted_binary" <<\'PY\' || fail\n'
        )
        start = text.index(marker) + len(marker)
        verifier = text[start : text.index("\nPY\n", start)]

        def tar_bytes(entries: list[tuple[str, bytes, int]]) -> bytes:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
                for name, raw, mode in entries:
                    info = tarfile.TarInfo(name)
                    info.size = len(raw)
                    info.mode = mode
                    info.uid = 0
                    info.gid = 0
                    info.mtime = 0
                    archive.addfile(info, io.BytesIO(raw))
            return stream.getvalue()

        binary = b"synthetic-bound-static-installer"
        digest = "sha256:" + hashlib.sha256(binary).hexdigest()
        default_path = (
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )

        def image_archive(
            extra_layer_file: bool,
            working_directory: str = "/",
            environment: list[str] | None = None,
        ) -> tuple[bytes, str]:
            layer_entries = [
                ("propertyquarry-release-single-host-installer-v2", binary, 0o555)
            ]
            if extra_layer_file:
                layer_entries.append(("unexpected", b"reject", 0o444))
            layer = tar_bytes(layer_entries)
            config = json.dumps(
                {
                    "architecture": "amd64",
                    "config": {
                        "Cmd": None,
                        "Entrypoint": [
                            "/propertyquarry-release-single-host-installer-v2"
                        ],
                        "Env": environment,
                        "WorkingDir": working_directory,
                    },
                    "os": "linux",
                    "rootfs": {
                        "diff_ids": ["sha256:" + hashlib.sha256(layer).hexdigest()],
                        "type": "layers",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            image_id = "sha256:" + hashlib.sha256(config).hexdigest()
            config_name = "blobs/sha256/" + image_id.removeprefix("sha256:")
            layer_name = "blobs/sha256/" + hashlib.sha256(layer).hexdigest()
            manifest = json.dumps(
                [{"Config": config_name, "Layers": [layer_name], "RepoTags": None}],
                separators=(",", ":"),
            ).encode("ascii")
            return tar_bytes(
                [
                    ("manifest.json", manifest, 0o644),
                    (config_name, config, 0o644),
                    (layer_name, layer, 0o644),
                ]
            ), image_id

        with tempfile.TemporaryDirectory(prefix="pq-rootfs-verifier-test-") as directory:
            root = Path(directory)
            verifier_path = root / "verifier.py"
            verifier_path.write_text(verifier, encoding="utf-8")
            cases = (
                ("exact-defaults", False, "/", [default_path], 0),
                ("legacy-defaults", False, "", None, 0),
                ("extra-layer-file", True, "/", [default_path], 1),
                ("unexpected-working-directory", False, "/tmp", [default_path], 1),
                ("unexpected-environment", False, "/", [default_path, "EXTRA=1"], 1),
                ("normalized-directory-legacy-environment", False, "/", None, 1),
                ("legacy-directory-normalized-environment", False, "", [default_path], 1),
            )
            for name, extra, working_directory, environment, expected_return in cases:
                with self.subTest(case=name):
                    archive, image_id = image_archive(
                        extra, working_directory, environment
                    )
                    archive_path = root / f"image-{name}.tar"
                    output_path = root / f"installer-{name}"
                    archive_path.write_bytes(archive)
                    result = subprocess.run(
                        [
                            "python3",
                            os.fspath(verifier_path),
                            os.fspath(archive_path),
                            image_id,
                            digest,
                            str(len(binary)),
                            os.fspath(output_path),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected_return)
                    if expected_return == 0:
                        self.assertEqual(output_path.read_bytes(), binary)
                        self.assertEqual(output_path.stat().st_mode & 0o777, 0o555)

    def test_builder_is_fixed_to_local_default_and_checks_scratch_rootfs(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        for token in (
            'record.get("Driver") == "docker"',
            'node.get("Endpoint") == "default"',
            "docker buildx build --builder default --load --platform linux/amd64",
            "--network none --pull=false --no-cache --provenance=false --sbom=false",
            '--iidfile "$iid_one_path"',
            '--iidfile "$iid_two_path"',
            '"$tagged_image_one" == "$image_one"',
            '"$tagged_image_two" == "$image_two"',
            '"$rootfs_verifier" verify-rootfs',
            '"scratch_rootfs_verified": True',
            '"verified_local_docker_daemon": True',
        ):
            self.assertIn(token, text)
        self.assertLess(text.index('"$rootfs_verifier" verify-rootfs'), text.index("docker image tag"))

    def test_builder_cleanup_only_removes_tags_it_created_and_still_owns(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        function_start = text.index("remove_owned_tag_if_unchanged() {\n")
        function_end = text.index("\n}\n\ncleanup() {", function_start) + 3
        cleanup_start = text.index("cleanup() {\n", function_end)
        cleanup_end = text.index("\n}\ntrap cleanup EXIT", cleanup_start) + 3
        function = text[function_start:function_end]
        cleanup = text[cleanup_start:cleanup_end]
        self.assertIn('"${owned_tag_one:-}" "${owned_tag_one_image:-}"', cleanup)
        self.assertIn('"${owned_tag_two:-}" "${owned_tag_two_image:-}"', cleanup)
        self.assertNotIn('docker image rm "$tag_one"', cleanup)
        self.assertNotIn('docker image rm "$tag_two"', cleanup)

        collision_one = text.index(
            '[[ -z "$(docker image inspect --format \'{{.Id}}\' "$tag_one"'
        )
        build_one = text.index("docker buildx build --builder default", collision_one)
        own_one = text.index('owned_tag_one="$tag_one"', build_one)
        bind_one = text.index('"$tagged_image_one" == "$image_one"', build_one)
        collision_two = text.index(
            '[[ -z "$(docker image inspect --format \'{{.Id}}\' "$tag_two"'
        )
        build_two = text.index("docker buildx build --builder default", build_one + 1)
        own_two = text.index('owned_tag_two="$tag_two"', build_two)
        bind_two = text.index('"$tagged_image_two" == "$image_two"', build_two)
        self.assertLess(text.index('owned_tag_one=""'), collision_one)
        self.assertLess(text.index('owned_tag_two=""'), collision_two)
        self.assertLess(collision_one, build_one)
        self.assertLess(build_one, own_one)
        self.assertLess(bind_one, own_one)
        self.assertLess(collision_two, build_two)
        self.assertLess(build_two, own_two)
        self.assertLess(bind_two, own_two)

        with tempfile.TemporaryDirectory(prefix="pq-owned-tag-cleanup-test-") as directory:
            calls = Path(directory) / "calls"
            harness = (
                function
                + "\n"
                + r'''
docker() {
  printf '%s\n' "$*" >>"$PQ_TEST_CALLS"
  if [[ "$1" == "image" && "$2" == "inspect" ]]; then
    printf '%s\n' "$PQ_TEST_CURRENT_IMAGE"
  fi
  return 0
}
remove_owned_tag_if_unchanged "$1" "$2"
'''
            )

            def invoke(tag: str, expected: str, current: str) -> list[str]:
                calls.unlink(missing_ok=True)
                completed = subprocess.run(
                    ["bash", "-c", harness, "cleanup-test", tag, expected],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "PQ_TEST_CALLS": os.fspath(calls),
                        "PQ_TEST_CURRENT_IMAGE": current,
                    },
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []

            image = "sha256:" + "a" * 64
            other = "sha256:" + "b" * 64
            self.assertEqual(invoke("", "", other), [])
            repointed = invoke("build-tag:fixture", image, other)
            self.assertEqual(len(repointed), 1)
            self.assertIn("image inspect", repointed[0])
            owned = invoke("build-tag:fixture", image, image)
            self.assertEqual(len(owned), 2)
            self.assertIn("image inspect", owned[0])
            self.assertEqual(owned[1], "image rm build-tag:fixture")

    def test_builder_binds_owned_tag_to_exact_buildx_iid_file(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        function_start = text.index("read_build_image_id() {\n")
        function_end = text.index("\n}\n\ncleanup() {", function_start) + 3
        function = text[function_start:function_end]
        self.assertIn('tag_nonce="${workspace##*-}"', text)
        self.assertIn('^sha256:[0-9a-f]{64}$', text)

        with tempfile.TemporaryDirectory(prefix="pq-iid-file-test-") as directory:
            iid_path = Path(directory) / "image.id"
            image = "sha256:" + "c" * 64
            harness = function + '\nread_build_image_id "$1"\n'
            for suffix in (b"", b"\n"):
                with self.subTest(newline=bool(suffix)):
                    iid_path.write_bytes(image.encode("ascii") + suffix)
                    completed = subprocess.run(
                        ["bash", "-c", harness, "iid-test", os.fspath(iid_path)],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env={"PATH": "/usr/bin:/bin"},
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout, image)
            iid_path.write_bytes(image.encode("ascii") + b"\nextra")
            rejected = subprocess.run(
                ["bash", "-c", harness, "iid-test", os.fspath(iid_path)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"PATH": "/usr/bin:/bin"},
            )
            self.assertNotEqual(rejected.returncode, 0)


if __name__ == "__main__":
    unittest.main()
