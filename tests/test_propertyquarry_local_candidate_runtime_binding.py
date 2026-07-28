from __future__ import annotations

import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

import pytest

from scripts import propertyquarry_local_candidate_runtime_binding as binding


CANDIDATE = "1" * 40
ENVELOPE = "2" * 40
CANDIDATE_TREE = "3" * 40
ENVELOPE_TREE = "4" * 40
IMAGE_CONFIG_ID = "sha256:" + "6" * 64
ROOTFS_DIFF_IDS = ["sha256:" + "5" * 64, "sha256:" + "9" * 64]
LOCAL_OCI_CONFIG_SIZE = 317
LOCAL_OCI_LAYERS = [
    {
        "media_type": binding.OCI_LAYER_MEDIA_TYPE,
        "digest": ROOTFS_DIFF_IDS[0],
        "size": 101,
    },
    {
        "media_type": binding.OCI_LAYER_MEDIA_TYPE,
        "digest": ROOTFS_DIFF_IDS[1],
        "size": 202,
    },
]
_OCI_MANIFEST = {
    "schemaVersion": 2,
    "mediaType": binding.OCI_MANIFEST_MEDIA_TYPE,
    "config": {
        "mediaType": binding.OCI_CONFIG_MEDIA_TYPE,
        "digest": IMAGE_CONFIG_ID,
        "size": LOCAL_OCI_CONFIG_SIZE,
    },
    "layers": [
        {
            "mediaType": item["media_type"],
            "digest": item["digest"],
            "size": item["size"],
        }
        for item in LOCAL_OCI_LAYERS
    ],
}
_OCI_MANIFEST_RAW = json.dumps(
    _OCI_MANIFEST, sort_keys=True, separators=(",", ":")
).encode("ascii")
OCI_DIGEST = "sha256:" + hashlib.sha256(_OCI_MANIFEST_RAW).hexdigest()
CONTAINER_ID = "7" * 64
MANIFEST_SHA = "8" * 64
IMAGE_REFERENCE = IMAGE_CONFIG_ID
OBSERVED_REPO_DIGEST = "propertyquarry@" + OCI_DIGEST
BASE_DIGEST = "sha256:" + "a" * 64
BASE_REFERENCE = "python:3.12-slim@" + BASE_DIGEST
BASE_IMAGE_ID = "sha256:" + "b" * 64
REPLICA_ID = "pq-local-test"
VERSION_URL = "http://127.0.0.1:18097/version"
DATA_VOLUME_NAME = "propertyquarry-data"
DATA_VOLUME_SOURCE = f"/var/lib/docker/volumes/{DATA_VOLUME_NAME}/_data"
CANDIDATE_ARCHIVE = b"candidate archive bytes"
ENVELOPE_ARCHIVE = b"metadata envelope archive bytes"
IMAGE_ENTRYPOINT = ["/usr/bin/python3"]
IMAGE_COMMAND = ["-m", "ea.app"]
IMAGE_ENVIRONMENT = ["BASE_SETTING=expected", "UNRELATED=value"]
HEALTHCHECK = {
    "Test": ["CMD", "/usr/bin/python3", "-m", "ea.healthcheck"],
    "Interval": 30_000_000_000,
    "Timeout": 5_000_000_000,
    "Retries": 3,
}
IMAGE_SHELL = ["/bin/sh", "-c"]
_REAL_VALIDATE_LOCAL_DOCKER_SOCKET = binding._validate_local_docker_socket


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class World:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.git_status = b""
        self.ancestor_returncode = 0
        self.head = ENVELOPE
        self.candidate_tree = CANDIDATE_TREE
        self.envelope_tree = ENVELOPE_TREE
        self.changed_paths = [binding.RELEASE_MANIFEST_PATH]
        self.candidate_archive = CANDIDATE_ARCHIVE
        self.envelope_archive = ENVELOPE_ARCHIVE
        self.fail_command: str | None = None
        self.failure_stdout = b""
        self.failure_stderr = b""
        self.status_calls = 0
        self.status_after: bytes | None = None
        self.image_calls = 0
        self.container_calls = 0
        self.version_calls = 0
        self.image_after: dict[str, Any] | None = None
        self.container_after: dict[str, Any] | None = None
        self.version_after: dict[str, Any] | bytes | None = None
        self.version_url_after: str | None = None
        self.image: dict[str, Any] = {
            "Id": IMAGE_CONFIG_ID,
            "RepoTags": ["propertyquarry:test-candidate"],
            "RepoDigests": [],
            "RootFS": {"Type": "layers", "Layers": list(ROOTFS_DIFF_IDS)},
            "Config": {
                "Entrypoint": IMAGE_ENTRYPOINT,
                "Cmd": IMAGE_COMMAND,
                "Shell": IMAGE_SHELL,
                "User": "1000:1000",
                "WorkingDir": "/app",
                "Healthcheck": HEALTHCHECK,
                "Env": IMAGE_ENVIRONMENT,
                "Labels": {
                    "org.opencontainers.image.revision": CANDIDATE,
                    "com.propertyquarry.metadata-envelope": ENVELOPE,
                    "com.propertyquarry.release-manifest-sha256": MANIFEST_SHA,
                }
            },
        }
        self.container: dict[str, Any] = {
            "Id": CONTAINER_ID,
            "Image": IMAGE_CONFIG_ID,
            "AppArmorProfile": "docker-default",
            "Platform": "linux",
            "Path": IMAGE_ENTRYPOINT[0],
            "Args": [*IMAGE_ENTRYPOINT[1:], *IMAGE_COMMAND],
            "RestartCount": 0,
            "State": {
                "Status": "running",
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "OOMKilled": False,
                "Dead": False,
                "ExitCode": 0,
                "Error": "",
                "Pid": 4242,
                "StartedAt": "2026-07-18T00:00:00.123456789Z",
                "Health": {"Status": "healthy", "FailingStreak": 0, "Log": []},
            },
            "Config": {
                "Image": IMAGE_REFERENCE,
                "Hostname": REPLICA_ID,
                "ExposedPorts": {"8090/tcp": {}},
                "Entrypoint": IMAGE_ENTRYPOINT,
                "Cmd": IMAGE_COMMAND,
                "Shell": IMAGE_SHELL,
                "User": "1000:1000",
                "WorkingDir": "/app",
                "Healthcheck": HEALTHCHECK,
                "Env": [
                    *IMAGE_ENVIRONMENT,
                    "PROPERTYQUARRY_RELEASE_COMMIT_SHA=" + CANDIDATE,
                    "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST=" + OCI_DIGEST,
                    "PROPERTYQUARRY_RELEASE_MANIFEST_SHA256=" + MANIFEST_SHA,
                ],
            },
            "HostConfig": {
                "NetworkMode": "bridge",
                "Privileged": False,
                "ReadonlyRootfs": True,
                "AutoRemove": False,
                "OomKillDisable": False,
                "Init": True,
                "PublishAllPorts": False,
                "PidMode": "",
                "IpcMode": "private",
                "UTSMode": "",
                "UsernsMode": "",
                "CgroupnsMode": "private",
                "Runtime": "runc",
                "Isolation": "",
                "VolumeDriver": "",
                "CgroupParent": "",
                "Cgroup": "",
                "ContainerIDFile": "",
                "CapAdd": [],
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Binds": [],
                "Links": [],
                "ExtraHosts": [],
                "Dns": [],
                "DnsOptions": [],
                "DnsSearch": [],
                "VolumesFrom": [],
                "GroupAdd": [],
                "DeviceCgroupRules": [],
                "Devices": [],
                "DeviceRequests": [],
                "Mounts": [],
                "Tmpfs": {},
                "Sysctls": {},
                "StorageOpt": {},
                "MaskedPaths": sorted(binding._REQUIRED_MASKED_PATHS),
                "ReadonlyPaths": sorted(binding._REQUIRED_READONLY_PATHS),
                "PortBindings": {
                    "8090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18097"}]
                },
            },
            "NetworkSettings": {
                "Ports": {
                    "8090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18097"}]
                },
                "Networks": {
                    "bridge": {
                        "NetworkID": "a" * 64,
                        "EndpointID": "b" * 64,
                        "IPAddress": "172.18.0.2",
                        "GlobalIPv6Address": "",
                    }
                },
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": DATA_VOLUME_NAME,
                    "Source": DATA_VOLUME_SOURCE,
                    "Destination": binding.AUTHORIZED_DATA_VOLUME_DESTINATION,
                    "Driver": binding.AUTHORIZED_DATA_VOLUME_DRIVER,
                    "Mode": "rw",
                    "RW": True,
                    "Propagation": "",
                }
            ],
        }
        self.version: dict[str, Any] = {
            "release_commit_sha": CANDIDATE,
            "release_manifest_sha256": MANIFEST_SHA,
            "release_image_digest": OCI_DIGEST,
            "replica_id": REPLICA_ID,
            "release_manifest_status": "complete",
            "release_manifest_errors": [],
            "release_manifest_mismatch_fields": [],
        }


class FakeExecutor:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_output_bytes: int,
    ) -> binding.CommandResult:
        del timeout_s, max_output_bytes
        command = tuple(argv)
        self.calls.append(command)
        if command[0] == "/usr/bin/git":
            arguments = command[command.index("-C") + 2 :]
            name = arguments[0]
            if self.world.fail_command == name:
                return binding.CommandResult(
                    returncode=17,
                    stdout=self.world.failure_stdout,
                    stderr=self.world.failure_stderr,
                )
            if name == "status":
                self.world.status_calls += 1
                output = self.world.git_status
                if self.world.status_calls > 1 and self.world.status_after is not None:
                    output = self.world.status_after
                return binding.CommandResult(0, output, b"")
            if name == "cat-file":
                return binding.CommandResult(0, b"", b"")
            if name == "merge-base":
                return binding.CommandResult(self.world.ancestor_returncode, b"", b"")
            if name == "rev-parse":
                revision = arguments[1]
                if revision == "--path-format=absolute":
                    value = str(self.world.repo_root / ".git" / "info" / "attributes")
                elif revision == "HEAD":
                    value = self.world.head
                elif revision == CANDIDATE + "^{tree}":
                    value = self.world.candidate_tree
                elif revision == ENVELOPE + "^{tree}":
                    value = self.world.envelope_tree
                else:
                    raise AssertionError(command)
                return binding.CommandResult(0, (value + "\n").encode("ascii"), b"")
            if name == "diff":
                output = b"".join(path.encode("utf-8") + b"\0" for path in self.world.changed_paths)
                return binding.CommandResult(0, output, b"")
            if name == "archive":
                revision = arguments[-1]
                if revision == CANDIDATE:
                    output = self.world.candidate_archive
                elif revision == ENVELOPE:
                    output = self.world.envelope_archive
                else:
                    raise AssertionError(command)
                return binding.CommandResult(0, output, b"")
            raise AssertionError(command)

        if command[0] == "/usr/bin/docker":
            assert command[1:3] == ("--host", f"unix://{binding.LOCAL_DOCKER_SOCKET}")
            arguments = command[3:]
            name = arguments[0] + "-" + arguments[1]
            if self.world.fail_command == name:
                return binding.CommandResult(
                    returncode=17,
                    stdout=self.world.failure_stdout,
                    stderr=self.world.failure_stderr,
                )
            if arguments[:2] == ("image", "inspect"):
                self.world.image_calls += 1
                value = self.world.image
                if self.world.image_calls > 1 and self.world.image_after is not None:
                    value = self.world.image_after
                return binding.CommandResult(0, json_bytes([value]), b"")
            if arguments[:2] == ("container", "inspect"):
                self.world.container_calls += 1
                value = self.world.container
                if self.world.container_calls > 1 and self.world.container_after is not None:
                    value = self.world.container_after
                return binding.CommandResult(0, json_bytes([value]), b"")
            raise AssertionError(command)
        raise AssertionError(command)


class FakeVersionFetcher:
    def __init__(self, world: World) -> None:
        self.world = world

    def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        max_output_bytes: int,
    ) -> binding.VersionResponse:
        del timeout_s, max_output_bytes
        self.world.version_calls += 1
        value: dict[str, Any] | bytes = self.world.version
        final_url = url
        if self.world.version_calls > 1:
            if self.world.version_after is not None:
                value = self.world.version_after
            if self.world.version_url_after is not None:
                final_url = self.world.version_url_after
        body = value if isinstance(value, bytes) else json_bytes(value)
        parsed = binding._local_version_endpoint(url)
        return binding.VersionResponse(
            status_code=200,
            final_url=final_url,
            body=body,
            peer_ip=str(parsed[0]),
            peer_port=parsed[1],
        )


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        (self.root / "ea").mkdir(parents=True)
        (self.root / "docs").mkdir(parents=True)
        self.dockerfile_bytes = b"FROM scratch\n"
        self.manifest_bytes = b"# generated release manifest\n"
        (self.root / binding.DOCKERFILE_PATH).write_bytes(self.dockerfile_bytes)
        (self.root / binding.RELEASE_MANIFEST_PATH).write_bytes(self.manifest_bytes)
        self.receipt_root = tmp_path / "receipts"
        self.receipt_root.mkdir(mode=0o700)
        self.build_path = self.receipt_root / "build-receipt.json"
        self.output_path = self.receipt_root / "binding-receipt.json"
        self.build_receipt: dict[str, Any] = {
            "schema": binding.BUILD_RECEIPT_SCHEMA,
            "generated_at": "2026-07-18T11:59:00Z",
            "source_candidate_sha": CANDIDATE,
            "metadata_envelope_sha": ENVELOPE,
            "source_candidate": {
                "tree_sha": CANDIDATE_TREE,
                "archive_sha256": digest(CANDIDATE_ARCHIVE),
            },
            "metadata_envelope": {
                "tree_sha": ENVELOPE_TREE,
                "archive_sha256": digest(ENVELOPE_ARCHIVE),
                "changed_paths": [binding.RELEASE_MANIFEST_PATH],
            },
            "dockerfile": {
                "path": binding.DOCKERFILE_PATH,
                "sha256": digest(self.dockerfile_bytes),
            },
            "release_manifest": {
                "path": binding.RELEASE_MANIFEST_PATH,
                "file_sha256": digest(self.manifest_bytes),
                "manifest_sha256": MANIFEST_SHA,
            },
            "base_images": [
                {
                    "reference": BASE_REFERENCE,
                    "image_config_id": BASE_IMAGE_ID,
                    "observed_repo_digest": "python@" + BASE_DIGEST,
                }
            ],
            "local_build": {
                "image_tag": "propertyquarry-local-candidate:fixture",
                "platform": "linux/amd64",
                "network_mode": "none",
                "pull": False,
                "docker_daemon_id_sha256": "sha256:" + "c" * 64,
            },
            "image_reference": IMAGE_REFERENCE,
            "oci_manifest_digest": OCI_DIGEST,
            "image_config_id": IMAGE_CONFIG_ID,
            "local_oci_manifest": {
                "construction": binding.OCI_CONSTRUCTION,
                "docker_archive_sha256": "sha256:" + "d" * 64,
                "media_type": binding.OCI_MANIFEST_MEDIA_TYPE,
                "manifest_size": len(_OCI_MANIFEST_RAW),
                "config": {
                    "media_type": binding.OCI_CONFIG_MEDIA_TYPE,
                    "digest": IMAGE_CONFIG_ID,
                    "size": LOCAL_OCI_CONFIG_SIZE,
                },
                "layers": copy.deepcopy(LOCAL_OCI_LAYERS),
            },
            "labels": {
                "org.opencontainers.image.revision": CANDIDATE,
                "com.propertyquarry.metadata-envelope": ENVELOPE,
                "com.propertyquarry.release-manifest-sha256": MANIFEST_SHA,
            },
            "authority": {
                "local_only": True,
                "performs_local_docker_build": True,
                "authoritative_for_release_effects": False,
                "public_launch_authority": False,
                "production_ready": False,
            },
        }
        self.world = World(self.root)
        self.expected_host_config_sha256 = binding._fingerprint(
            self.world.container["HostConfig"]
        )
        self.rewrite_build_receipt()

    def rewrite_build_receipt(self) -> None:
        self.build_raw = binding._canonical_json_bytes(self.build_receipt)
        self.build_path.write_bytes(self.build_raw)

    def config(self, **overrides: Any) -> binding.BindingConfig:
        values: dict[str, Any] = {
            "repo_root": self.root,
            "receipt_root": self.receipt_root,
            "source_candidate_sha": CANDIDATE,
            "metadata_envelope_sha": ENVELOPE,
            "build_receipt_path": self.build_path,
            "expected_build_receipt_sha256": digest(self.build_raw),
            "image_reference": IMAGE_REFERENCE,
            "container_id": CONTAINER_ID,
            "expected_replica_id": REPLICA_ID,
            "authorized_data_volume_name": DATA_VOLUME_NAME,
            "authorized_data_volume_source": DATA_VOLUME_SOURCE,
            "expected_host_config_sha256": self.expected_host_config_sha256,
            "version_url": VERSION_URL,
            "receipt_path": self.output_path,
        }
        values.update(overrides)
        return binding.BindingConfig(**values)

    def verify(self, config: binding.BindingConfig | None = None) -> binding.VerificationResult:
        return binding.verify_binding(
            config or self.config(),
            executor=FakeExecutor(self.world),
            version_fetcher=FakeVersionFetcher(self.world),
            now=lambda: dt.datetime(2026, 7, 17, 12, 0, tzinfo=dt.timezone.utc),
        )

    def fails(
        self,
        code: str,
        config: binding.BindingConfig | None = None,
    ) -> binding.BindingError:
        with pytest.raises(binding.BindingError) as raised:
            self.verify(config)
        assert raised.value.code == code
        assert str(raised.value) == code
        assert not self.output_path.exists()
        return raised.value


@pytest.fixture(autouse=True)
def _avoid_live_docker_socket_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binding, "_validate_local_docker_socket", lambda: None)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_emits_private_local_only_receipt_after_two_matching_observations(
    harness: Harness,
) -> None:
    result = harness.verify()

    receipt = json.loads(harness.output_path.read_text(encoding="utf-8"))
    assert receipt == result.receipt
    assert receipt["status"] == "pass"
    assert receipt["gate_passed"] is True
    assert receipt["local_docker_authority"] is True
    assert receipt["production_authority"] is False
    assert receipt["public_launch_authority"] is False
    assert receipt["authority"] == {
        "local_docker_authority": True,
        "production_authority": False,
        "public_launch_authority": False,
    }
    assert receipt["production_ready"] is False
    assert receipt["performs_release_effects"] is False
    assert receipt["oci_manifest_digest"] == OCI_DIGEST
    assert receipt["observed_repo_digest"] is None
    assert receipt["image_config_id"] == IMAGE_CONFIG_ID
    assert receipt["oci_manifest_digest"] != receipt["image_config_id"]
    assert receipt["checks"]["registry_independent_local_oci_matches"] is True
    assert receipt["host_config_sha256"] == harness.expected_host_config_sha256
    assert receipt["verified_mounts"] == [
        {
            "destination": binding.AUTHORIZED_DATA_VOLUME_DESTINATION,
            "driver": binding.AUTHORIZED_DATA_VOLUME_DRIVER,
            "identity_sha256": receipt["verified_mounts"][0]["identity_sha256"],
            "name": DATA_VOLUME_NAME,
            "read_only": False,
            "source_sha256": digest(DATA_VOLUME_SOURCE.encode("utf-8")),
            "type": "volume",
        }
    ]
    assert result.receipt_sha256 == digest(harness.output_path.read_bytes())
    assert stat.S_IMODE(harness.output_path.stat().st_mode) == 0o600
    assert harness.world.image_calls == 2
    assert harness.world.container_calls == 2
    assert harness.world.version_calls == 2
    assert harness.world.status_calls == 2


def test_dirty_build_context_fails_without_receipt(harness: Harness) -> None:
    harness.world.git_status = b" M ea/app/main.py\0"
    harness.fails("dirty_build_context")


def test_local_git_info_attributes_cannot_rewrite_archive_evidence(
    harness: Harness,
) -> None:
    attributes = harness.root / ".git" / "info" / "attributes"
    attributes.parent.mkdir(parents=True)
    attributes.write_text("ea/app export-ignore\n", encoding="utf-8")
    harness.fails("git_local_attributes_forbidden")


def test_non_descendant_metadata_envelope_fails(harness: Harness) -> None:
    harness.world.ancestor_returncode = 1
    harness.fails("metadata_envelope_not_descendant")


def test_metadata_descendant_with_source_change_fails(harness: Harness) -> None:
    harness.world.changed_paths.append("ea/app/main.py")
    harness.fails("metadata_envelope_contains_source_changes")


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("label", "docker_image_label_mismatch"),
        ("repo_digest", "docker_repo_digest_conflicts_with_local_oci"),
        ("image", "docker_image_config_id_mismatch"),
        ("container_image", "container_image_config_id_mismatch"),
    ],
)
def test_label_digest_and_image_substitution_fail_closed(
    harness: Harness,
    mutation: str,
    expected_code: str,
) -> None:
    if mutation == "label":
        harness.world.image["Config"]["Labels"]["org.opencontainers.image.revision"] = "9" * 40
    elif mutation == "repo_digest":
        harness.world.image["RepoDigests"] = ["propertyquarry@sha256:" + "a" * 64]
    elif mutation == "image":
        harness.world.image["Id"] = "sha256:" + "a" * 64
    else:
        harness.world.container["Image"] = "sha256:" + "a" * 64
    harness.fails(expected_code)


@pytest.mark.parametrize("version_body", [b"", b"{}"])
def test_empty_version_evidence_fails(harness: Harness, version_body: bytes) -> None:
    harness.world.version_after = None
    harness.world.version = {} if version_body == b"{}" else {}  # type: ignore[assignment]

    class EmptyFetcher(FakeVersionFetcher):
        def fetch(
            self,
            url: str,
            *,
            timeout_s: float,
            max_output_bytes: int,
        ) -> binding.VersionResponse:
            del timeout_s, max_output_bytes
            address, port = binding._local_version_endpoint(url)
            return binding.VersionResponse(200, url, version_body, str(address), port)

    with pytest.raises(binding.BindingError) as raised:
        binding.verify_binding(
            harness.config(),
            executor=FakeExecutor(harness.world),
            version_fetcher=EmptyFetcher(harness.world),
        )
    assert raised.value.code in {"invalid_version_json", "version_identity_mismatch"}
    assert not harness.output_path.exists()


def test_forged_version_identity_fails(harness: Harness) -> None:
    harness.world.version["release_commit_sha"] = "9" * 40
    harness.fails("version_identity_mismatch")


@pytest.mark.parametrize(
    "destination", ["/", "/app", "/app/services", "//app", "//app/services", "/app/"]
)
def test_app_replacement_mount_fails(harness: Harness, destination: str) -> None:
    harness.world.container["Mounts"].append(
        {"Destination": destination, "Type": "bind", "RW": False}
    )
    harness.fails("app_replacement_mount")


def test_toctou_image_change_during_reinspection_fails(harness: Harness) -> None:
    harness.world.image_after = copy.deepcopy(harness.world.image)
    harness.world.image_after["Id"] = "sha256:" + "a" * 64
    harness.fails("toctou_reinspection_failed")


def test_toctou_git_change_during_reinspection_fails(harness: Harness) -> None:
    harness.world.status_after = b" M ea/app/main.py\0"
    harness.fails("toctou_reinspection_failed")


def test_toctou_version_change_during_reinspection_fails(harness: Harness) -> None:
    harness.world.version_after = copy.deepcopy(harness.world.version)
    assert isinstance(harness.world.version_after, dict)
    harness.world.version_after["replica_id"] = "substituted-replica"
    harness.fails("toctou_reinspection_failed")


def test_command_failure_redacts_both_output_streams(harness: Harness) -> None:
    secret = "pq-secret-should-never-escape"
    harness.world.fail_command = "status"
    harness.world.failure_stdout = ("stdout:" + secret).encode()
    harness.world.failure_stderr = ("stderr:" + secret).encode()
    error = harness.fails("git_status_failed")
    assert secret not in str(error)
    assert secret not in repr(error)


def test_invalid_version_body_is_redacted(harness: Harness) -> None:
    secret = "version-secret-should-never-escape"
    harness.world.version = {}  # kept unused by the custom fetcher

    class SecretFetcher(FakeVersionFetcher):
        def fetch(
            self,
            url: str,
            *,
            timeout_s: float,
            max_output_bytes: int,
        ) -> binding.VersionResponse:
            del timeout_s, max_output_bytes
            address, port = binding._local_version_endpoint(url)
            return binding.VersionResponse(
                200, url, ("not-json:" + secret).encode(), str(address), port
            )

    with pytest.raises(binding.BindingError) as raised:
        binding.verify_binding(
            harness.config(),
            executor=FakeExecutor(harness.world),
            version_fetcher=SecretFetcher(harness.world),
        )
    assert raised.value.code == "invalid_version_json"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert not harness.output_path.exists()


def test_injected_command_output_is_still_bounded(harness: Harness) -> None:
    harness.world.git_status = b"x" * 65
    harness.fails("command_output_limit", harness.config(max_command_output_bytes=64))


def test_injected_version_output_is_still_bounded(harness: Harness) -> None:
    harness.world.version = {}  # kept unused by the custom fetcher

    class LargeFetcher(FakeVersionFetcher):
        def fetch(
            self,
            url: str,
            *,
            timeout_s: float,
            max_output_bytes: int,
        ) -> binding.VersionResponse:
            del timeout_s, max_output_bytes
            address, port = binding._local_version_endpoint(url)
            return binding.VersionResponse(200, url, b"x" * 65, str(address), port)

    with pytest.raises(binding.BindingError) as raised:
        binding.verify_binding(
            harness.config(max_version_output_bytes=64),
            executor=FakeExecutor(harness.world),
            version_fetcher=LargeFetcher(harness.world),
        )
    assert raised.value.code == "version_output_limit"
    assert not harness.output_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_timeout_s", binding._MAX_COMMAND_TIMEOUT_S + 1),
        ("http_timeout_s", binding._MAX_HTTP_TIMEOUT_S + 1),
        ("max_command_output_bytes", binding._MAX_COMMAND_OUTPUT_BYTES + 1),
        ("max_archive_output_bytes", binding._MAX_ARCHIVE_OUTPUT_BYTES + 1),
        ("max_version_output_bytes", binding._MAX_VERSION_OUTPUT_BYTES + 1),
        ("max_build_receipt_bytes", binding._MAX_BUILD_RECEIPT_BYTES + 1),
        ("max_input_file_bytes", binding._MAX_INPUT_FILE_BYTES + 1),
        ("command_timeout_s", float("nan")),
        ("max_command_output_bytes", True),
    ],
)
def test_resource_limits_have_non_overridable_hard_maxima(
    harness: Harness, field: str, value: Any
) -> None:
    harness.fails("invalid_resource_limit", harness.config(**{field: value}))
    assert harness.world.status_calls == 0
    assert harness.world.image_calls == 0
    assert harness.world.container_calls == 0
    assert harness.world.version_calls == 0


def test_pinned_build_receipt_hash_is_required(harness: Harness) -> None:
    harness.build_path.write_bytes(harness.build_raw + b" ")
    harness.fails("build_receipt_hash_mismatch")


def test_build_input_hash_change_fails(harness: Harness) -> None:
    (harness.root / binding.DOCKERFILE_PATH).write_bytes(b"FROM substituted\n")
    harness.fails("dockerfile_hash_mismatch")


def test_oci_manifest_digest_must_not_be_image_config_id(harness: Harness) -> None:
    harness.build_receipt["oci_manifest_digest"] = IMAGE_CONFIG_ID
    harness.rewrite_build_receipt()
    harness.fails("oci_digest_equals_image_config_id", harness.config())


def test_build_receipt_must_be_canonical_json_with_one_trailing_newline(
    harness: Harness,
) -> None:
    raw = json.dumps(harness.build_receipt, indent=2, sort_keys=False).encode("utf-8")
    assert raw != binding._canonical_json_bytes(harness.build_receipt)
    harness.build_path.write_bytes(raw)
    harness.fails(
        "build_receipt_not_canonical",
        harness.config(expected_build_receipt_sha256=digest(raw)),
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("production_ready", True, "invalid_build_authority"),
        ("public_launch_authority", True, "invalid_build_authority"),
        ("authoritative_for_release_effects", True, "invalid_build_authority"),
        ("local_only", False, "invalid_build_authority"),
    ],
)
def test_build_receipt_never_conveys_release_or_public_authority(
    harness: Harness,
    field: str,
    value: bool,
    expected_code: str,
) -> None:
    harness.build_receipt["authority"][field] = value
    harness.rewrite_build_receipt()
    harness.fails(expected_code, harness.config())


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("manifest_size", "local_oci_manifest_mismatch"),
        ("layer_digest", "local_oci_manifest_mismatch"),
        ("config_digest", "invalid_local_oci_evidence"),
        ("construction", "invalid_local_oci_evidence"),
        ("archive_digest", "invalid_local_oci_evidence"),
    ],
)
def test_registry_independent_oci_receipt_is_closed_and_self_consistent(
    harness: Harness,
    mutation: str,
    expected_code: str,
) -> None:
    local = harness.build_receipt["local_oci_manifest"]
    if mutation == "manifest_size":
        local["manifest_size"] += 1
    elif mutation == "layer_digest":
        local["layers"][0]["digest"] = "sha256:" + "e" * 64
    elif mutation == "config_digest":
        local["config"]["digest"] = "sha256:" + "e" * 64
    elif mutation == "construction":
        local["construction"] = "registry-derived"
    else:
        local["docker_archive_sha256"] = "not-a-digest"
    harness.rewrite_build_receipt()
    harness.fails(expected_code, harness.config())


def test_runtime_image_rootfs_must_match_locally_derived_layer_diff_ids(
    harness: Harness,
) -> None:
    harness.world.image["RootFS"]["Layers"][0] = "sha256:" + "e" * 64
    harness.fails("docker_image_rootfs_mismatch")


def test_build_receipt_metadata_paths_must_match_git_observation(harness: Harness) -> None:
    harness.build_receipt["metadata_envelope"]["changed_paths"] = []
    harness.rewrite_build_receipt()
    harness.fails("metadata_envelope_path_evidence_mismatch", harness.config())


def test_partial_failure_never_overwrites_an_existing_receipt(harness: Harness) -> None:
    harness.output_path.write_text("operator-owned\n", encoding="utf-8")
    with pytest.raises(binding.BindingError) as raised:
        harness.verify()
    assert raised.value.code == "receipt_already_exists"
    assert harness.output_path.read_text(encoding="utf-8") == "operator-owned\n"


def test_partial_atomic_write_leaves_no_success_receipt(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated durability failure")

    monkeypatch.setattr(binding.os, "fsync", fail_fsync)
    with pytest.raises(binding.BindingError) as raised:
        harness.verify()
    assert raised.value.code == "receipt_write_failed"
    assert not harness.output_path.exists()
    assert not any(
        path.name.startswith("." + harness.output_path.name + ".")
        for path in harness.output_path.parent.iterdir()
    )


def test_atomic_receipt_link_interrupt_removes_exact_inode(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = binding.os.link

    def link_then_interrupt(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(binding.os, "link", link_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        binding._atomic_write_new_private(
            harness.output_path.parent,
            harness.output_path,
            b"{}\n",
        )
    assert not harness.output_path.exists()
    assert not any(
        path.name.startswith("." + harness.output_path.name + ".")
        for path in harness.output_path.parent.iterdir()
    )


def test_public_or_non_loopback_version_url_is_rejected(harness: Harness) -> None:
    harness.fails(
        "version_url_not_local",
        harness.config(version_url="http://203.0.113.1:443/version"),
    )


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def _http_server(
    host: str,
    chunks: list[bytes],
    *,
    delays: list[float] | None = None,
):
    requests: list[bytes] = []
    delays = delays or [0.0] * len(chunks)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            request = bytearray()
            self.request.settimeout(1.0)
            with contextlib.suppress(OSError):
                while b"\r\n\r\n" not in request:
                    part = self.request.recv(4096)
                    if not part:
                        return
                    request.extend(part)
                requests.append(bytes(request))
                length = sum(len(chunk) for chunk in chunks)
                self.request.sendall(
                    (
                        "HTTP/1.1 200 OK\r\n"
                        f"Content-Length: {length}\r\n"
                        "Content-Type: application/json\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                for delay, chunk in zip(delays, chunks, strict=True):
                    if delay:
                        time.sleep(delay)
                    self.request.sendall(chunk)

    server = _ThreadingTCPServer((host, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield int(server.server_address[1]), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        (binding.TRUSTED_GIT_BIN, "-C", str(repo), *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"HOME": str(repo.parent), "LANG": "C", "LC_ALL": "C", "PATH": ""},
        check=True,
        timeout=5,
    )
    return result.stdout


def test_changed_paths_rejects_raw_newline_aliases() -> None:
    allowed = binding.RELEASE_MANIFEST_PATH.encode("utf-8")
    for raw in (allowed + b"\n\0", b"\n" + allowed + b"\0", b"\n\0"):
        with pytest.raises(binding.BindingError) as raised:
            binding._changed_paths(raw)
        assert raised.value.code == "invalid_git_path_output"


def test_tree_diff_catches_source_change_introduced_by_merge(
    harness: Harness,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "merge-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "gate@example.invalid")
    _git(repo, "config", "user.name", "Gate Test")
    (repo / "docs").mkdir()
    (repo / "ea").mkdir()
    (repo / binding.RELEASE_MANIFEST_PATH).write_text("base\n", encoding="utf-8")
    (repo / "ea/app.py").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    candidate_tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    _git(repo, "checkout", "-q", "-b", "metadata")
    (repo / binding.RELEASE_MANIFEST_PATH).write_text("metadata\n", encoding="utf-8")
    _git(repo, "add", binding.RELEASE_MANIFEST_PATH)
    _git(repo, "commit", "-q", "-m", "metadata")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "metadata")
    (repo / "ea/app.py").write_text("merge-only source mutation\n", encoding="utf-8")
    _git(repo, "add", "ea/app.py")
    _git(repo, "commit", "-q", "-m", "merge with source mutation")
    envelope_tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()

    evidence = binding._Evidence(
        harness.config(repo_root=repo), binding.BoundedSubprocessExecutor()
    )
    changed = binding._tree_changed_paths(evidence, candidate_tree, envelope_tree)
    assert changed == (binding.RELEASE_MANIFEST_PATH, "ea/app.py")


def test_local_fetcher_ignores_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    with _http_server("127.0.0.1", [b"proxy"]) as (proxy_port, proxy_requests):
        with _http_server("127.0.0.2", [b"direct"]) as (target_port, target_requests):
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
            monkeypatch.setenv("no_proxy", "")
            monkeypatch.setenv("NO_PROXY", "")
            url = f"http://127.0.0.2:{target_port}/version"
            response = binding.LocalVersionFetcher().fetch(
                url, timeout_s=1.0, max_output_bytes=64
            )
    assert response.body == b"direct"
    assert response.peer_ip == "127.0.0.2"
    assert response.peer_port == target_port
    assert target_requests
    assert proxy_requests == []


def test_local_fetcher_enforces_total_slow_drip_deadline() -> None:
    with _http_server("127.0.0.1", [b"x"] * 5, delays=[0.1] * 5) as (port, _requests):
        started = time.monotonic()
        with pytest.raises(binding.BindingError) as raised:
            binding.LocalVersionFetcher().fetch(
                f"http://127.0.0.1:{port}/version",
                timeout_s=0.15,
                max_output_bytes=64,
            )
        elapsed = time.monotonic() - started
    assert raised.value.code == "version_fetch_timeout"
    assert elapsed < 0.4


def test_forged_version_peer_fails(harness: Harness) -> None:
    class WrongPeer(FakeVersionFetcher):
        def fetch(
            self,
            url: str,
            *,
            timeout_s: float,
            max_output_bytes: int,
        ) -> binding.VersionResponse:
            response = super().fetch(
                url, timeout_s=timeout_s, max_output_bytes=max_output_bytes
            )
            return binding.VersionResponse(
                response.status_code,
                response.final_url,
                response.body,
                "127.0.0.2",
                response.peer_port,
            )

    with pytest.raises(binding.BindingError) as raised:
        binding.verify_binding(
            harness.config(),
            executor=FakeExecutor(harness.world),
            version_fetcher=WrongPeer(harness.world),
        )
    assert raised.value.code == "version_peer_mismatch"
    assert not harness.output_path.exists()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("port", "version_container_port_mismatch"),
        ("wildcard", "version_container_port_mismatch"),
        ("ambiguous", "version_container_port_ambiguous"),
        ("host", "container_network_mode_unbound"),
        ("none", "container_network_mode_unbound"),
    ],
)
def test_version_endpoint_requires_exact_container_port_binding(
    harness: Harness,
    mutation: str,
    expected_code: str,
) -> None:
    if mutation in {"host", "none"}:
        harness.world.container["HostConfig"]["NetworkMode"] = mutation
    elif mutation == "port":
        for source in (
            harness.world.container["HostConfig"]["PortBindings"],
            harness.world.container["NetworkSettings"]["Ports"],
        ):
            source["8090/tcp"][0]["HostPort"] = "18098"
    elif mutation == "wildcard":
        for source in (
            harness.world.container["HostConfig"]["PortBindings"],
            harness.world.container["NetworkSettings"]["Ports"],
        ):
            source["8090/tcp"][0]["HostIp"] = "0.0.0.0"
    else:
        duplicate = [{"HostIp": "127.0.0.1", "HostPort": "18097"}]
        harness.world.container["HostConfig"]["PortBindings"]["8091/tcp"] = duplicate
        harness.world.container["NetworkSettings"]["Ports"]["8091/tcp"] = copy.deepcopy(
            duplicate
        )
    harness.fails(expected_code)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("extra_observed_unbound", "version_container_port_ambiguous"),
        ("version_observed_unbound", "version_container_port_mismatch"),
        ("extra_configured_unbound", "version_container_port_ambiguous"),
        ("extra_exposed", "version_container_port_ambiguous"),
        ("missing_exposed", "version_container_port_ambiguous"),
    ],
)
def test_version_endpoint_rejects_every_extra_or_unbound_port_surface(
    harness: Harness,
    mutation: str,
    expected_code: str,
) -> None:
    container = harness.world.container
    if mutation == "extra_observed_unbound":
        container["NetworkSettings"]["Ports"]["8091/tcp"] = None
    elif mutation == "version_observed_unbound":
        container["NetworkSettings"]["Ports"]["8090/tcp"] = None
    elif mutation == "extra_configured_unbound":
        container["HostConfig"]["PortBindings"]["8091/tcp"] = None
    elif mutation == "extra_exposed":
        container["Config"]["ExposedPorts"]["8091/tcp"] = {}
    else:
        container["Config"]["ExposedPorts"] = {}
    harness.fails(expected_code)


def test_immutable_id_with_empty_local_repo_digests_is_registry_independent(
    harness: Harness,
) -> None:
    harness.fails(
        "invalid_image_reference",
        harness.config(image_reference="propertyquarry:test-candidate"),
    )
    harness.world.image["RepoDigests"] = []
    result = harness.verify()
    assert result.receipt["observed_repo_digest"] is None


def test_matching_repo_digest_is_accepted_when_daemon_has_one(harness: Harness) -> None:
    harness.world.image["RepoDigests"] = [OBSERVED_REPO_DIGEST]
    result = harness.verify()
    assert result.receipt["observed_repo_digest"] == OBSERVED_REPO_DIGEST


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("command", "container_command_override"),
        ("shell", "container_shell_override"),
        ("path", "container_process_override"),
        ("user", "container_runtime_contract_mismatch"),
        ("healthcheck", "container_healthcheck_override"),
        ("environment", "container_runtime_environment_mismatch"),
    ],
)
def test_container_runtime_overrides_fail(
    harness: Harness,
    mutation: str,
    expected_code: str,
) -> None:
    container = harness.world.container
    if mutation == "command":
        container["Config"]["Cmd"] = ["-m", "substituted"]
    elif mutation == "shell":
        container["Config"]["Shell"] = ["/bin/bash", "-c"]
    elif mutation == "path":
        container["Path"] = "/bin/false"
    elif mutation == "user":
        container["Config"]["User"] = "root"
    elif mutation == "healthcheck":
        container["Config"]["Healthcheck"] = {"Test": ["CMD", "/bin/true"]}
    else:
        container["Config"]["Env"].append("UNAUTHORIZED_SECRET=value")
    harness.fails(expected_code)


def test_untrusted_command_binary_is_rejected_before_launch() -> None:
    with pytest.raises(binding.BindingError) as raised:
        binding.BoundedSubprocessExecutor().run(
            ("/tmp/propertyquarry-fake-docker",),
            timeout_s=1,
            max_output_bytes=64,
        )
    assert raised.value.code == "command_binary_untrusted"


def test_process_group_kill_is_proven() -> None:
    process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(60)"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert binding._process_group_exists(process.pid)
        binding._kill_process(process)
        assert process.poll() is not None
        assert not binding._process_group_exists(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_receipt_paths_require_private_nonsymlink_root(
    harness: Harness,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    harness.fails("receipt_outside_root", harness.config(receipt_path=outside))

    actual = tmp_path / "actual-receipts"
    actual.mkdir(mode=0o700)
    alias = tmp_path / "receipt-alias"
    alias.symlink_to(actual, target_is_directory=True)
    alias_build = alias / "build.json"
    alias_build.write_bytes(harness.build_raw)
    alias_output = alias / "output.json"
    harness.fails(
        "receipt_root_invalid",
        harness.config(
            receipt_root=alias,
            build_receipt_path=alias_build,
            receipt_path=alias_output,
        ),
    )

    harness.receipt_root.chmod(0o755)
    harness.fails("receipt_root_invalid")


def test_post_link_durability_failure_removes_success_receipt(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fsync = binding.os.fsync
    calls = 0

    def fail_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory durability failure")
        real_fsync(descriptor)

    monkeypatch.setattr(binding.os, "fsync", fail_second_fsync)
    with pytest.raises(binding.BindingError) as raised:
        harness.verify()
    assert raised.value.code == "receipt_write_failed"
    assert not harness.output_path.exists()
    assert not any(
        path.name.startswith("." + harness.output_path.name + ".")
        for path in harness.receipt_root.iterdir()
    )


def test_git_evidence_ignores_replacements_and_local_fsmonitor(
    harness: Harness,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "hermetic-git"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "gate@example.invalid")
    _git(repo, "config", "user.name", "Gate Test")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "original")
    original_commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    original_tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()

    (repo / "tracked.txt").write_text("replacement\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "replacement")
    replacement_commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    _git(repo, "replace", original_commit, replacement_commit)

    marker = tmp_path / "fsmonitor-ran"
    monitor = tmp_path / "fsmonitor.sh"
    monitor.write_text(
        "#!/bin/sh\n"
        f"/usr/bin/touch {marker}\n"
        "printf '\\0'\n",
        encoding="utf-8",
    )
    monitor.chmod(0o700)
    _git(repo, "config", "core.fsmonitor", str(monitor))

    evidence = binding._Evidence(
        harness.config(repo_root=repo), binding.BoundedSubprocessExecutor()
    )
    observed_tree = evidence.git(
        "rev-parse",
        original_commit + "^{tree}",
        error_code="tree_failed",
    ).stdout.decode().strip()
    evidence.git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        error_code="status_failed",
    )

    assert observed_tree == original_tree
    assert not marker.exists()
    assert binding.BoundedSubprocessExecutor._ENVIRONMENT["GIT_CONFIG"] == "/dev/null"
    assert binding.BoundedSubprocessExecutor._ENVIRONMENT["GIT_NO_LAZY_FETCH"] == "1"
    assert binding.BoundedSubprocessExecutor._ENVIRONMENT["GIT_NO_REPLACE_OBJECTS"] == "1"


@pytest.mark.parametrize(
    "field",
    [
        "Privileged",
        "ReadonlyRootfs",
        "AutoRemove",
        "OomKillDisable",
        "Init",
        "PublishAllPorts",
        "PidMode",
        "IpcMode",
        "UTSMode",
        "UsernsMode",
        "CgroupnsMode",
        "Runtime",
        "Isolation",
        "VolumeDriver",
        "CgroupParent",
        "Cgroup",
        "ContainerIDFile",
        "CapAdd",
        "CapDrop",
        "SecurityOpt",
        "Binds",
        "Links",
        "ExtraHosts",
        "Dns",
        "DnsOptions",
        "DnsSearch",
        "VolumesFrom",
        "GroupAdd",
        "DeviceCgroupRules",
        "Devices",
        "DeviceRequests",
        "Mounts",
        "Tmpfs",
        "Sysctls",
        "StorageOpt",
        "MaskedPaths",
        "ReadonlyPaths",
    ],
)
def test_every_closed_host_security_field_rejects_mutation(
    harness: Harness,
    field: str,
) -> None:
    host_config = harness.world.container["HostConfig"]
    value = host_config[field]
    if isinstance(value, bool):
        host_config[field] = not value
    elif isinstance(value, str):
        host_config[field] = value + "substituted"
    elif isinstance(value, dict):
        value["substituted"] = "value"
    elif field in {"Devices", "DeviceRequests", "Mounts"}:
        value.append({"PathOnHost": "/dev/substituted"})
    elif field in {"MaskedPaths", "ReadonlyPaths"}:
        value.pop()
    else:
        value.append("substituted")
    harness.fails("container_host_security_mismatch")


@pytest.mark.parametrize("field", ["Pid", "StartedAt", "RestartCount"])
def test_container_process_identity_is_fingerprinted_across_inspections(
    harness: Harness,
    field: str,
) -> None:
    harness.world.container_after = copy.deepcopy(harness.world.container)
    if field == "Pid":
        harness.world.container_after["State"]["Pid"] = 5252
    elif field == "StartedAt":
        harness.world.container_after["State"]["StartedAt"] = "2026-07-18T00:00:01Z"
    else:
        harness.world.container_after["RestartCount"] = 1
    harness.fails("toctou_evidence_changed")


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("extra", "unauthorized_container_mounts"),
        ("destination", "authorized_data_volume_identity_mismatch"),
        ("type", "authorized_data_volume_identity_mismatch"),
        ("mode", "authorized_data_volume_identity_mismatch"),
    ],
)
def test_only_explicitly_authorized_mount_contract_is_accepted(
    harness: Harness,
    mutation: str,
    expected_code: str,
) -> None:
    mount = harness.world.container["Mounts"][0]
    if mutation == "extra":
        harness.world.container["Mounts"].append(
            {"Destination": "/tmp", "Type": "tmpfs", "RW": True}
        )
    elif mutation == "destination":
        mount["Destination"] = "/var/lib/substituted"
    elif mutation == "type":
        mount["Type"] = "bind"
    else:
        mount["RW"] = False
    harness.fails(expected_code)


@pytest.mark.parametrize("field", ["Name", "Source", "Driver"])
def test_authorized_data_volume_identity_rejects_first_observation_substitution(
    harness: Harness,
    field: str,
) -> None:
    mount = harness.world.container["Mounts"][0]
    if field == "Name":
        mount[field] = "substituted-volume"
    elif field == "Source":
        mount[field] = "/var/lib/docker/volumes/substituted-volume/_data"
    else:
        mount[field] = "substituted-driver"
    harness.fails("authorized_data_volume_identity_mismatch")


@pytest.mark.parametrize("field", ["Name", "Source", "Driver"])
def test_authorized_data_volume_identity_rejects_second_observation_substitution(
    harness: Harness,
    field: str,
) -> None:
    harness.world.container_after = copy.deepcopy(harness.world.container)
    mount = harness.world.container_after["Mounts"][0]
    if field == "Name":
        mount[field] = "substituted-volume"
    elif field == "Source":
        mount[field] = "/var/lib/docker/volumes/substituted-volume/_data"
    else:
        mount[field] = "substituted-driver"
    harness.fails("toctou_reinspection_failed")


@pytest.mark.parametrize(
    "mutation",
    [
        {"LogConfig": {"Type": "syslog", "Config": {}}},
        {"RestartPolicy": {"Name": "always", "MaximumRetryCount": 0}},
        {"ShmSize": 134_217_728},
        {"Ulimits": [{"Name": "nofile", "Soft": 1_048_576, "Hard": 1_048_576}]},
    ],
)
def test_operator_host_config_digest_closes_version_specific_fields(
    harness: Harness,
    mutation: dict[str, Any],
) -> None:
    harness.world.container["HostConfig"].update(copy.deepcopy(mutation))
    harness.fails("host_config_digest_mismatch")


def test_operator_host_config_digest_catches_second_observation_change(
    harness: Harness,
) -> None:
    harness.world.container_after = copy.deepcopy(harness.world.container)
    harness.world.container_after["HostConfig"]["LogConfig"] = {
        "Type": "syslog",
        "Config": {},
    }
    harness.fails("toctou_reinspection_failed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AppArmorProfile", ""),
        ("Platform", "windows"),
    ],
)
def test_adjacent_container_security_identity_is_closed(
    harness: Harness,
    field: str,
    value: str,
) -> None:
    harness.world.container[field] = value
    harness.fails("container_host_security_mismatch")


def test_docker_evidence_uses_only_explicit_local_unix_socket(harness: Harness) -> None:
    executor = FakeExecutor(harness.world)
    evidence = binding._Evidence(harness.config(), executor)
    evidence.docker("image", "inspect", IMAGE_REFERENCE, error_code="inspect_failed")
    assert executor.calls == [
        (
            binding.TRUSTED_DOCKER_BIN,
            "--host",
            f"unix://{binding.LOCAL_DOCKER_SOCKET}",
            "image",
            "inspect",
            IMAGE_REFERENCE,
        )
    ]


def test_local_docker_socket_must_be_a_trusted_unix_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "docker.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        socket_path.chmod(0o660)
        monkeypatch.setattr(binding, "LOCAL_DOCKER_SOCKET", socket_path)
        _REAL_VALIDATE_LOCAL_DOCKER_SOCKET()
    finally:
        server.close()
    socket_path.unlink()
    socket_path.write_text("not a socket", encoding="utf-8")
    with pytest.raises(binding.BindingError) as raised:
        _REAL_VALIDATE_LOCAL_DOCKER_SOCKET()
    assert raised.value.code == "docker_socket_untrusted"
