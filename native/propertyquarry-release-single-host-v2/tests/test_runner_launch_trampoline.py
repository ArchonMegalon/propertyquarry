#!/usr/bin/env python3
"""Fail-closed tests for the fixed ephemeral-runner launch trampoline."""

from __future__ import annotations

import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
WRAPPER = TOOLS / "launch-ephemeral-runner-with-docker.sh"
RELAY_PATH = TOOLS / "relay-runner-admin-token.py"
PACKAGE_PATH = TOOLS / "package.py"
LIFECYCLE = TOOLS / "run-ephemeral-runner-with-docker.sh"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


relay = load_module("propertyquarry_runner_token_relay", RELAY_PATH)
package = load_module("propertyquarry_runner_launch_package", PACKAGE_PATH)


class RunnerLaunchWrapperTests(unittest.TestCase):
    def test_shell_syntax_and_invalid_argv_fail_before_docker(self) -> None:
        subprocess.run(["bash", "-n", os.fspath(WRAPPER)], check=True)
        for arguments in ((), ("a", "b", "c", "generic-command")):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["bash", os.fspath(WRAPPER), *arguments],
                    env={
                        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
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
                    "propertyquarry-docker-runner-launch-rejected\n",
                )

    def test_only_token_broker_inherits_caller_fd_and_release_is_gated(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        broker = text.index("/usr/bin/python3 -I -B \"$token_relay\"")
        close = text.index("exec 8<&-", broker)
        image_verify = text.index('"$image_verifier" verify-image', close)
        create = text.index("docker create --pull never -i", image_verify)
        inspect = text.index('docker inspect "$container_id"', create)
        release = text.index("printf '%s\\n' release >&7", inspect)
        start = text.index(
            'docker start --attach --interactive "$container_id"',
            release,
        )
        self.assertLess(broker, close)
        self.assertLess(close, image_verify)
        self.assertLess(image_verify, create)
        self.assertLess(create, inspect)
        self.assertLess(inspect, release)
        self.assertLess(release, start)
        self.assertIn(
            "--token-fd 8 --gate-fd 7 --status-fd 9 "
            '--relay-fifo "$relay_fifo"',
            text,
        )
        self.assertIn('[[ "$token_fd_marker" == "8" && -p /proc/self/fd/8 ]]', text)
        self.assertIn("runner-admin-token-ready", text)
        self.assertIn("verify_file_contracts", text[inspect:release])

    def test_container_is_exact_fixed_mode_with_minimal_root_envelope(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        start = text.index("docker create --pull never -i")
        end = text.index(')" || fail', start)
        create = text[start:end]
        for required in (
            "--network bridge",
            "--read-only --user 0:0",
            "--entrypoint /propertyquarry-release-single-host-installer-v2",
            "--cap-drop ALL",
            "--cap-add CHOWN",
            "--cap-add DAC_OVERRIDE",
            "--cap-add FOWNER",
            "--cap-add SYS_CHROOT",
            "--security-opt no-new-privileges",
            "--pids-limit 128",
            "--memory 256m --memory-swap 256m --cpus 1",
            "--ulimit core=0:0",
            "--log-driver none",
            "type=bind,src=/,dst=/host,readonly=false,bind-propagation=rslave",
            "dst=/input/propertyquarry-release-single-host-v2.tar,readonly",
            "dst=/host/run/systemd/resolve/stub-resolv.conf,readonly",
            '"$helper_image_id" launch-ephemeral-runner',
        ):
            self.assertIn(required, create)
        self.assertEqual(create.count("--cap-add"), 4)
        for forbidden in (
            "--privileged",
            "--pid host",
            "--network host",
            "--network none",
            "/var/run/docker.sock",
            "bash -c",
            "sh -c",
            "eval ",
            "PROPERTYQUARRY_RUNNER_ADMIN_TOKEN",
            "ACTIONS_RUNNER_INPUT_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        ):
            self.assertNotIn(forbidden, create)

    def test_inspect_contract_rejects_cap_network_mount_and_command_drift(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        for required in (
            'assert item.get("Path") == '
            '"/propertyquarry-release-single-host-installer-v2"',
            'assert item.get("Args") == ["launch-ephemeral-runner"]',
            'assert host.get("ReadonlyRootfs") is True',
            'assert host.get("Privileged") is False',
            'assert host.get("CapDrop") == ["ALL"]',
            'assert sorted(host.get("CapAdd") or []) == [',
            '"CAP_SYS_CHROOT",',
            'assert host.get("NetworkMode") == "bridge"',
            'assert host.get("PidMode") in (None, "")',
            'assert host_mount.get("Source") == "/"',
            'assert host_mount.get("RW") is True',
            'assert host_mount.get("Propagation") == "rslave"',
            'assert package_mount.get("Source") == package_path',
            'assert package_mount.get("RW") is False',
            '"/host/run/systemd/resolve/stub-resolv.conf"',
            'assert resolver_mount.get("Source") == resolver_path',
            'assert resolver_mount.get("RW") is False',
            'assert isinstance(networks, dict) and set(networks) == {"bridge"}',
            'assert "/var/run/docker.sock" not in {',
        ):
            self.assertIn(required, text)

    def test_extracted_inspector_accepts_only_daemon_normalized_envelope(
        self,
    ) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        call = text.index(
            "/usr/bin/python3 -I -B -",
            text.index('docker inspect "$container_id"'),
        )
        start = text.index("import json\n", call)
        verifier = text[start : text.index("\nPY\n", start)]
        container_id = "a" * 64
        image_id = "sha256:" + "b" * 64
        name = "propertyquarry-test-envelope"
        package_path = "/tmp/package.tar"
        resolver_path = "/tmp/bridge-resolv.conf"
        exact = [
            {
                "Args": ["launch-ephemeral-runner"],
                "Config": {
                    "AttachStdin": True,
                    "Cmd": ["launch-ephemeral-runner"],
                    "Entrypoint": [
                        "/propertyquarry-release-single-host-installer-v2"
                    ],
                    "Env": [
                        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:"
                        "/usr/bin:/sbin:/bin"
                    ],
                    "Image": image_id,
                    "OpenStdin": True,
                    "Tty": False,
                    "User": "0:0",
                },
                "HostConfig": {
                    "AutoRemove": False,
                    "Binds": None,
                    "CapAdd": [
                        "CAP_CHOWN",
                        "CAP_DAC_OVERRIDE",
                        "CAP_FOWNER",
                        "CAP_SYS_CHROOT",
                    ],
                    "CapDrop": ["ALL"],
                    "Devices": [],
                    "GroupAdd": None,
                    "LogConfig": {"Config": {}, "Type": "none"},
                    "Memory": 268435456,
                    "MemorySwap": 268435456,
                    "NanoCpus": 1000000000,
                    "NetworkMode": "bridge",
                    "PidMode": "",
                    "PidsLimit": 128,
                    "Privileged": False,
                    "ReadonlyRootfs": True,
                    "RestartPolicy": {
                        "MaximumRetryCount": 0,
                        "Name": "no",
                    },
                    "SecurityOpt": ["no-new-privileges"],
                    "Ulimits": [
                        {"Hard": 0, "Name": "core", "Soft": 0}
                    ],
                },
                "Id": container_id,
                "Mounts": [
                    {
                        "Destination": "/host",
                        "Propagation": "rslave",
                        "RW": True,
                        "Source": "/",
                        "Type": "bind",
                    },
                    {
                        "Destination": (
                            "/input/"
                            "propertyquarry-release-single-host-v2.tar"
                        ),
                        "RW": False,
                        "Source": package_path,
                        "Type": "bind",
                    },
                    {
                        "Destination": (
                            "/host/run/systemd/resolve/stub-resolv.conf"
                        ),
                        "RW": False,
                        "Source": resolver_path,
                        "Type": "bind",
                    },
                ],
                "Name": "/" + name,
                "NetworkSettings": {"Networks": {"bridge": {}}},
                "Path": (
                    "/propertyquarry-release-single-host-installer-v2"
                ),
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier_path = root / "inspect-verifier.py"
            verifier_path.write_text(verifier, encoding="utf-8")

            def run(value: list[dict[str, object]]) -> int:
                inspect_path = root / "inspect.json"
                inspect_path.write_text(
                    json.dumps(value),
                    encoding="utf-8",
                )
                return subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        os.fspath(verifier_path),
                        os.fspath(inspect_path),
                        container_id,
                        name,
                        image_id,
                        package_path,
                        resolver_path,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                ).returncode

            self.assertEqual(run(exact), 0)
            mutations = (
                lambda value: value[0].update({"Args": ["/bin/sh"]}),
                lambda value: value[0]["HostConfig"].update(
                    {"CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SYS_CHROOT"]}
                ),
                lambda value: value[0]["HostConfig"].update(
                    {
                        "CapAdd": value[0]["HostConfig"]["CapAdd"]
                        + ["CAP_NET_ADMIN"]
                    }
                ),
                lambda value: value[0]["HostConfig"].update(
                    {"NetworkMode": "host"}
                ),
                lambda value: value[0]["HostConfig"].update(
                    {"PidMode": "host"}
                ),
                lambda value: value[0]["Mounts"][1].update({"RW": True}),
                lambda value: value[0]["Mounts"][2].update(
                    {"Source": "/tmp/substituted"}
                ),
                lambda value: value[0]["Mounts"].append(
                    {
                        "Destination": "/var/run/docker.sock",
                        "RW": True,
                        "Source": "/var/run/docker.sock",
                        "Type": "bind",
                    }
                ),
                lambda value: value[0]["Config"].update(
                    {"Env": ["GITHUB_TOKEN=secret"]}
                ),
            )
            for mutate in mutations:
                candidate = copy.deepcopy(exact)
                mutate(candidate)
                self.assertNotEqual(run(candidate), 0)

    def test_package_image_and_paths_are_pinned_and_rechecked(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        for required in (
            r"^sha256:[0-9a-f]{64}$",
            '"400:1000:1000:1"',
            '"444:1000:1000:1"',
            "capture_file_contract",
            "package_identity",
            "anchor_identity",
            "package_digest",
            "anchor_digest",
            "resolver_identity",
            "nameserver 127.0.0.11",
            "486c423f110722ca4217f91dda8a187e07d4ac8ac08d8d7ed4f59f51abc1ac3d",
            'sha256sum -- "$package_path"',
            'sha256sum -- "$package_anchor_path"',
            'docker image inspect --format \'{{.Id}}\'',
        ):
            self.assertIn(required, text)
        self.assertGreaterEqual(text.count("verify_file_contracts"), 4)
        self.assertGreaterEqual(
            text.count(
                'docker image inspect --format \'{{.Id}}\''
            ),
            2,
        )
        self.assertIn('"$image_verifier" verify-image', text)
        self.assertNotIn("tcp://", text)
        self.assertNotIn("ssh://", text)

    def test_bridge_resolver_survives_chroot_for_github_https(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        helper = (
            ROOT / "internal/installhelper/runner_launch.go"
        ).read_text(encoding="utf-8")
        supervisor = (
            ROOT / "internal/authority/runner_supervisor.go"
        ).read_text(encoding="utf-8")
        self.assertIn("--network bridge", wrapper)
        self.assertIn("nameserver 127.0.0.11", wrapper)
        self.assertIn(
            "dst=/host/run/systemd/resolve/stub-resolv.conf,readonly",
            wrapper,
        )
        self.assertIn("fixedRunnerResolverLink", helper)
        self.assertIn(
            '"../run/systemd/resolve/stub-resolv.conf"',
            helper,
        )
        self.assertIn("validateRunnerLaunchResolver", helper)
        self.assertIn("https://api.github.com", supervisor)
        self.assertNotIn("--network host", wrapper)

    def test_signed_package_binds_exact_root_lifecycle_member(self) -> None:
        self.assertEqual(
            package.PAYLOAD_LAYOUT[package.RUNNER_LIFECYCLE_INSTALL_PATH],
            ("ephemeral-runner-root-lifecycle", 0o555),
        )
        lock_raw, launcher, lifecycle = package.load_runner_assets()
        self.assertEqual(lifecycle, LIFECYCLE.read_bytes())
        package.validate_runner_assets(lock_raw, launcher, lifecycle)
        mutations = (
            lifecycle.replace(b'[[ "$#" -eq 0 ]]', b'[[ "$#" -ge 0 ]]'),
            lifecycle.replace(b"/proc/self/fd/8", b"/tmp/token"),
            lifecycle.replace(
                b"exec 8<&-",
                b": # descriptor left open",
                1,
            ),
            lifecycle.replace(
                b'"release-supervisor"',
                b'"release-early"',
                1,
            ),
            lifecycle.replace(
                b"export PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8",
                b"export PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8\n"
                b"/usr/bin/true",
                1,
            ),
            lifecycle + b"bash -c true\n",
            lifecycle + b"sh -c true\n",
            lifecycle + b"eval true\n",
        )
        for mutated in mutations:
            with self.subTest(mutation=mutated[-32:]):
                with self.assertRaises(package.PackageFailure):
                    package.validate_runner_assets(lock_raw, launcher, mutated)

    def test_lifecycle_broker_hides_token_capability_until_exact_release(
        self,
    ) -> None:
        lifecycle = LIFECYCLE.read_text(encoding="utf-8")
        start = lifecycle.index("start_runner_token_broker() {")
        end_marker = "# End fixed runner token broker."
        end = lifecycle.index(end_marker, start) + len(end_marker)
        broker_function = lifecycle[start:end]
        with tempfile.TemporaryDirectory() as directory:
            controller = Path(directory) / "controller"
            controller.write_text(
                """#!/bin/bash
set -euo pipefail
[[ "$#" -eq 1 && "$1" == "runner-supervise" ]]
[[ "${PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD:-}" == "8" ]]
[[ -p /proc/self/fd/8 ]]
IFS= read -r token <&8
printf 'SUPERVISOR:%s\\n' "$token"
""",
                encoding="utf-8",
            )
            controller.chmod(0o755)
            harness = f"""\
set -euo pipefail
fail() {{ exit 50; }}
controller={os.fspath(controller)!r}
{broker_function}
start_runner_token_broker
[[ ! -e /proc/self/fd/8 ]]
/usr/bin/python3 -c '
import os, sys
assert "PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD" not in os.environ
for raw in sys.argv[1:]:
    try:
        os.fstat(int(raw))
    except OSError:
        continue
    raise SystemExit(51)
' "$supervisor_gate_fd" "$supervisor_output_fd"
kill -0 "$supervisor_pid"
printf '%s\\n' 'release-supervisor' >&"$supervisor_gate_fd"
exec {{supervisor_gate_fd}}>&-
IFS= read -r observed <&"$supervisor_output_fd"
wait "$supervisor_pid"
printf '%s\\n' "$observed"
"""
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "exec 8<&0\nexec 0</dev/null\n" + harness,
                ],
                input="s" * 20 + "\n",
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "TZ": "UTC",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout,
                "SUPERVISOR:" + "s" * 20 + "\n",
            )


class RunnerTokenRelayTests(unittest.TestCase):
    def caller_identity(self):
        return mock.patch.multiple(
            relay,
            CALLER_UID=os.getuid(),
            CALLER_GID=os.getgid(),
        )

    @staticmethod
    def pipe_with(raw: bytes) -> int:
        read_descriptor, write_descriptor = os.pipe2(os.O_CLOEXEC)
        try:
            os.write(write_descriptor, raw)
        finally:
            os.close(write_descriptor)
        return read_descriptor

    def test_token_reader_accepts_one_exact_fifo_token_only(self) -> None:
        token = b"a" * 20
        for raw in (token, token + b"\n"):
            descriptor = self.pipe_with(raw)
            try:
                with self.caller_identity():
                    observed = relay.read_token_fd(descriptor, 0.5)
                self.assertEqual(observed, bytearray(token))
            finally:
                os.close(descriptor)
        for raw in (
            b"a" * 19,
            token + b"\n\n",
            token + b"!",
            b"a" * 2049,
        ):
            descriptor = self.pipe_with(raw)
            try:
                with self.caller_identity():
                    with self.assertRaises(relay.RelayRejected):
                        relay.read_token_fd(descriptor, 0.5)
            finally:
                os.close(descriptor)

    def test_token_reader_rejects_regular_and_write_only_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "regular"
            path.write_bytes(b"a" * 20)
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                with self.caller_identity():
                    with self.assertRaises(relay.RelayRejected):
                        relay.read_token_fd(descriptor, 0.1)
            finally:
                os.close(descriptor)
        read_descriptor, write_descriptor = os.pipe2(os.O_CLOEXEC)
        try:
            with self.caller_identity():
                with self.assertRaises(relay.RelayRejected):
                    relay.require_fifo_fd(
                        write_descriptor,
                        access_mode=os.O_RDONLY,
                    )
        finally:
            os.close(read_descriptor)
            os.close(write_descriptor)

    def test_gate_is_exact_and_rejects_trailing_or_substituted_commands(self) -> None:
        for raw, accepted in (
            (b"release\n", True),
            (b"release", False),
            (b"release\nexec\n", False),
            (b"run\n", False),
        ):
            descriptor = self.pipe_with(raw)
            try:
                if accepted:
                    relay.read_exact_gate(descriptor)
                else:
                    with self.assertRaises(relay.RelayRejected):
                        relay.read_exact_gate(descriptor)
            finally:
                os.close(descriptor)

    def test_private_relay_fifo_gets_token_and_newline_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.fifo"
            os.mkfifo(path, 0o600)
            os.chmod(path, 0o600)
            observed: list[bytes] = []

            def read_fifo() -> None:
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    raw = bytearray()
                    while True:
                        chunk = os.read(descriptor, 4096)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    observed.append(bytes(raw))
                finally:
                    os.close(descriptor)

            reader = threading.Thread(target=read_fifo, daemon=True)
            reader.start()
            token = bytearray(b"b" * 20)
            with self.caller_identity():
                relay.write_token_fifo(os.fspath(path), token, 1.0)
            reader.join(timeout=2)
            self.assertFalse(reader.is_alive())
            self.assertEqual(observed, [b"b" * 20 + b"\n"])

    def test_relay_contract_is_exact_and_failure_status_is_generic(self) -> None:
        with mock.patch.object(relay, "disable_process_dumps"):
            with self.assertRaises(relay.RelayRejected):
                relay.relay(0, relay.GATE_FD, relay.STATUS_FD, "/tmp/fifo")
        read_descriptor, write_descriptor = os.pipe2(os.O_CLOEXEC)
        try:
            relay.failure_status(write_descriptor)
            os.close(write_descriptor)
            write_descriptor = -1
            self.assertEqual(
                os.read(read_descriptor, 4096),
                b"runner-admin-token-relay-rejected\n",
            )
        finally:
            os.close(read_descriptor)
            if write_descriptor >= 0:
                os.close(write_descriptor)
        source = RELAY_PATH.read_text(encoding="utf-8")
        self.assertIn("resource.RLIMIT_CORE", source)
        self.assertIn("PR_SET_DUMPABLE", source)
        self.assertIn("TOKEN_FD = 8", source)
        self.assertIn("CALLER_UID = 1000", source)
        self.assertIn("CALLER_GID = 1000", source)
        self.assertNotIn("print(token", source)
        self.assertNotIn("logging.", source)


if __name__ == "__main__":
    unittest.main()
