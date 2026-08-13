from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Sequence

import pytest

from scripts import propertyquarry_local_candidate_build as build
from scripts import verify_generated_release_artifacts_clean as manifest_model


CANDIDATE = "1" * 40
ENVELOPE = "2" * 40
CANDIDATE_TREE = "3" * 40
ENVELOPE_TREE = "4" * 40
BASE_DIGEST = "sha256:" + "5" * 64
BASE_REFERENCE = "python:3.12-slim@" + BASE_DIGEST
BASE_IMAGE_ID = "sha256:" + "6" * 64
DAEMON_ID = "local-daemon-fixture"
LOCAL_TAG = "propertyquarry-local-candidate:fixture"


def test_direct_cli_help_does_not_require_pythonpath() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "propertyquarry_local_candidate_build.py"),
            "--help",
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Build one authenticated PropertyQuarry candidate" in completed.stdout


def test_built_image_smoke_resolves_routes_through_fastapi_public_api() -> None:
    assert 'app.url_path_for("health_live")' in build.BUILT_IMAGE_SMOKE_SCRIPT
    assert "app.router.lifespan_context(app)" in build.BUILT_IMAGE_SMOKE_SCRIPT
    assert "for route in app.routes" not in build.BUILT_IMAGE_SMOKE_SCRIPT
    assert "app.router.startup()" not in build.BUILT_IMAGE_SMOKE_SCRIPT


def test_web_image_declares_the_smoke_enforced_entrypoint() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    dockerfile = (repository_root / build.DOCKERFILE_PATH).read_text(encoding="utf-8")
    entrypoint = json.dumps(list(build.BUILT_IMAGE_SMOKE_ENTRYPOINT))

    assert (
        "COPY --chmod=0555 scripts/property_web_entrypoint.py "
        "/usr/local/libexec/property_web_entrypoint.py"
    ) in dockerfile
    assert f"ENTRYPOINT {entrypoint}" in dockerfile


def test_web_image_entrypoint_execs_the_command_without_a_shell() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    entrypoint = repository_root / "scripts" / "property_web_entrypoint.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(entrypoint),
            sys.executable,
            "-c",
            "print('property-web-entrypoint-ok')",
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "property-web-entrypoint-ok"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def manifest_bytes(candidate: str = CANDIDATE) -> bytes:
    values = dict(manifest_model.RELEASE_MANIFEST_STATIC_VALUES)
    values.update(
        {
            "release_commit_sha": candidate,
            "release_artifact_set": (
                manifest_model.RELEASE_ARTIFACT_SET_PREFIX + "a" * 64
            ),
            "release_label": f"propertyquarry-source-browser-candidate-{candidate[:12]}",
            "release_deployment_id": f"propertyquarry-governed-deploy-{candidate[:12]}",
            "release_generated_at": "2026-07-16T18:08:35Z",
        }
    )
    body = json.dumps(values, sort_keys=True, indent=2)
    return (
        "# PropertyQuarry Release Manifest\n\n"
        + manifest_model.RELEASE_MANIFEST_JSON_START
        + "\n```json\n"
        + body
        + "\n```\n"
        + manifest_model.RELEASE_MANIFEST_JSON_END
        + "\n"
    ).encode("utf-8")


def tar_bytes(files: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        directories: set[str] = set()
        for path in files:
            parts = path.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                directories.add("/".join(parts[:index]))
        for path in sorted(directories):
            info = tarfile.TarInfo(path)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for path, payload in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            info.mode = 0o755 if path == build.DOCKERFILE_PATH else 0o644
            archive.addfile(info, io.BytesIO(payload))
        if symlink is not None:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            info.mode = 0o777
            archive.addfile(info)
    return output.getvalue()


def docker_archive(
    *,
    layers: list[bytes],
    labels: dict[str, str],
    config_mutation: dict[str, Any] | None = None,
    layer_payload_mutation: bytes | None = None,
    unsafe_symlink: bool = False,
    containerd_layout: bool = False,
    layer_source_size_delta: int = 0,
    containerd_legacy_config_path: bool = False,
    containerd_legacy_layer_paths: bool = False,
    containerd_omit_layer_sources: bool = False,
    layer_source_override: dict[str, Any] | None = None,
    extra_layer_source: bool = False,
) -> tuple[bytes, str, list[str]]:
    diff_ids = [digest(layer) for layer in layers]
    config: dict[str, Any] = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Labels": labels},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [],
    }
    if config_mutation:
        config.update(copy.deepcopy(config_mutation))
    config_raw = canonical(config)
    image_id = digest(config_raw)
    config_digest = image_id.removeprefix("sha256:")
    config_path = (
        f"blobs/sha256/{config_digest}"
        if containerd_layout and not containerd_legacy_config_path
        else config_digest + ".json"
    )
    layer_paths = (
        [f"blobs/sha256/{diff_id.removeprefix('sha256:')}" for diff_id in diff_ids]
        if containerd_layout and not containerd_legacy_layer_paths
        else [f"layer-{index}/layer.tar" for index in range(len(layers))]
    )
    manifest_record: dict[str, Any] = {
        "Config": config_path,
        "RepoTags": None if containerd_layout else [LOCAL_TAG],
        "Layers": layer_paths,
    }
    if containerd_layout and not containerd_omit_layer_sources:
        layer_sources: dict[str, Any] = {
            diff_id: {
                "mediaType": build.OCI_LAYER_MEDIA_TYPE,
                "size": len(layer) + layer_source_size_delta,
                "digest": diff_id,
            }
            for diff_id, layer in zip(diff_ids, layers, strict=True)
        }
        if layer_source_override:
            layer_sources[diff_ids[0]].update(copy.deepcopy(layer_source_override))
        if extra_layer_source:
            foreign_digest = "sha256:" + "9" * 64
            layer_sources[foreign_digest] = {
                "mediaType": build.OCI_LAYER_MEDIA_TYPE,
                "size": 1,
                "digest": foreign_digest,
            }
        manifest_record["LayerSources"] = layer_sources
    manifest = [manifest_record]
    files: dict[str, bytes] = {
        "manifest.json": canonical(manifest),
        config_path: config_raw,
    }
    for index, (path, payload) in enumerate(zip(layer_paths, layers, strict=True)):
        files[path] = layer_payload_mutation if index == 0 and layer_payload_mutation else payload
    if containerd_layout:
        unrelated = b"unrelated valid content-addressed metadata blob"
        files[f"blobs/sha256/{digest(unrelated).removeprefix('sha256:')}"] = unrelated
        files["index.json"] = canonical({"schemaVersion": 2, "manifests": []})
        files["oci-layout"] = canonical({"imageLayoutVersion": "1.0.0"})
    return (
        tar_bytes(files, symlink=("unsafe", "../../outside") if unsafe_symlink else None),
        image_id,
        diff_ids,
    )


class World:
    def __init__(self) -> None:
        self.dockerfile = f"FROM {BASE_REFERENCE}\nRUN echo fixture\n".encode("ascii")
        self.release_manifest = manifest_bytes()
        self.candidate_archive = tar_bytes(
            {
                build.DOCKERFILE_PATH: self.dockerfile,
                build.RELEASE_MANIFEST_PATH: b"old metadata manifest\n",
                "tracked.txt": b"candidate\n",
            }
        )
        self.envelope_archive = tar_bytes(
            {
                build.DOCKERFILE_PATH: self.dockerfile,
                build.RELEASE_MANIFEST_PATH: self.release_manifest,
                "tracked.txt": b"candidate\n",
            }
        )
        self.candidate_tree = CANDIDATE_TREE
        self.envelope_tree = ENVELOPE_TREE
        self.changed_paths = [build.RELEASE_MANIFEST_PATH]
        self.ancestor_returncode = 0
        self.git_observation = 0
        self.git_after: dict[str, Any] = {}
        self.daemon_ids = [DAEMON_ID, DAEMON_ID]
        self.base_after: dict[str, Any] | None = None
        self.image_after: dict[str, Any] | None = None
        self.tag_exists = False
        self.built = False
        self.tag_present = False
        self.build_input: bytes | None = None
        self.tamper_docker_config = False
        self.return_oversized_build_output = False
        self.smoke_container: dict[str, Any] | None = None
        self.smoke_container_id = "7" * 64
        self.smoke_returncode = 0
        self.smoke_stdout = build.BUILT_IMAGE_SMOKE_MARKER
        self.smoke_create_stdout = (self.smoke_container_id + "\n").encode("ascii")
        self.smoke_readonly_rootfs = True
        self.labels = {
            "org.opencontainers.image.revision": CANDIDATE,
            "com.propertyquarry.metadata-envelope": ENVELOPE,
            "com.propertyquarry.release-manifest-sha256": (
                manifest_model.release_manifest_sha256(
                    manifest_model._parse_release_manifest(
                        self.release_manifest.decode("utf-8")
                    )[0]
                )
            ),
        }
        self.layers = [b"first uncompressed layer tar", b"second layer tar"]
        self.docker_archive, self.image_id, self.diff_ids = docker_archive(
            layers=self.layers,
            labels=self.labels,
        )
        self.base_image = {
            "Id": BASE_IMAGE_ID,
            "RepoDigests": ["python@" + BASE_DIGEST],
            "Config": {"OnBuild": None},
        }
        self.image = {
            "Id": self.image_id,
            "Architecture": "amd64",
            "Os": "linux",
            "RepoTags": [LOCAL_TAG],
            "RepoDigests": [],
            "RootFS": {"Type": "layers", "Layers": self.diff_ids},
            "Config": {"Labels": self.labels},
        }


class FakeExecutor:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[dict[str, Any]] = []
        self.daemon_calls = 0
        self.base_calls = 0
        self.image_calls = 0

    def run(
        self,
        argv: Sequence[str],
        *,
        input_data: bytes | None,
        timeout_s: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> build.CommandResult:
        command = tuple(argv)
        self.calls.append(
            {
                "argv": command,
                "input": input_data,
                "timeout": timeout_s,
                "stdout_limit": max_stdout_bytes,
                "stderr_limit": max_stderr_bytes,
            }
        )
        if command[0] == build.TRUSTED_GIT_BIN:
            arguments = command[command.index("-C") + 2 :]
            operation = arguments[0]
            after = self.world.git_observation > 0
            overrides = self.world.git_after if after else {}
            if operation == "cat-file":
                return build.CommandResult(0, b"", b"")
            if operation == "merge-base":
                return build.CommandResult(self.world.ancestor_returncode, b"", b"")
            if operation == "rev-parse":
                revision = arguments[1]
                if revision == "--path-format=absolute":
                    repository = Path(command[command.index("-C") + 1])
                    value = str(repository / ".git" / "info" / "attributes")
                elif revision == CANDIDATE + "^{tree}":
                    value = overrides.get("candidate_tree", self.world.candidate_tree)
                elif revision == ENVELOPE + "^{tree}":
                    value = overrides.get("envelope_tree", self.world.envelope_tree)
                else:
                    raise AssertionError(command)
                return build.CommandResult(0, (value + "\n").encode("ascii"), b"")
            if operation == "diff":
                paths = overrides.get("changed_paths", self.world.changed_paths)
                output = b"".join(path.encode("utf-8") + b"\0" for path in paths)
                return build.CommandResult(0, output, b"")
            if operation == "archive":
                revision = arguments[-1]
                if revision == CANDIDATE:
                    payload = overrides.get("candidate_archive", self.world.candidate_archive)
                elif revision == ENVELOPE:
                    payload = overrides.get("envelope_archive", self.world.envelope_archive)
                    self.world.git_observation += 1
                else:
                    raise AssertionError(command)
                return build.CommandResult(0, payload, b"")
            raise AssertionError(command)

        assert command[0] == build.TRUSTED_DOCKER_BIN
        assert command[1] == "--config"
        docker_config = Path(command[2])
        assert command[3:5] == ("--host", f"unix://{build.LOCAL_DOCKER_SOCKET}")
        arguments = command[5:]
        if self.world.tamper_docker_config and arguments[:2] == ("image", "build"):
            (docker_config / "config.json").write_text('{"auths":{}}\n', encoding="utf-8")
        if arguments[:2] == ("system", "info"):
            value = self.world.daemon_ids[self.daemon_calls]
            self.daemon_calls += 1
            return build.CommandResult(0, canonical(value), b"")
        if arguments[:2] == ("image", "inspect"):
            reference = arguments[2]
            if reference == LOCAL_TAG and not self.world.tag_present:
                return build.CommandResult(0 if self.world.tag_exists else 1, b"", b"")
            if reference == BASE_REFERENCE:
                value = self.world.base_image
                if self.base_calls > 0 and self.world.base_after is not None:
                    value = self.world.base_after
                self.base_calls += 1
                return build.CommandResult(0, canonical([value]), b"")
            if reference in {LOCAL_TAG, self.world.image_id}:
                value = self.world.image
                if self.image_calls > 0 and self.world.image_after is not None:
                    value = self.world.image_after
                self.image_calls += 1
                return build.CommandResult(0, canonical([value]), b"")
            raise AssertionError(command)
        if arguments[:2] == ("image", "build"):
            self.world.build_input = input_data
            self.world.built = True
            self.world.tag_present = True
            output = (
                b"x" * (max_stdout_bytes + 1)
                if self.world.return_oversized_build_output
                else (self.world.image_id + "\n").encode("ascii")
            )
            return build.CommandResult(0, output, b"")
        if arguments[:2] == ("image", "save"):
            assert arguments[2] == self.world.image_id
            return build.CommandResult(0, self.world.docker_archive, b"")
        if arguments[:2] == ("container", "inspect"):
            reference = arguments[2]
            container = self.world.smoke_container
            if container is None or reference not in {
                container["Id"],
                str(container["Name"]).removeprefix("/"),
            }:
                return build.CommandResult(1, b"", b"")
            return build.CommandResult(0, canonical([container]), b"")
        if arguments[:2] == ("container", "create"):
            name = arguments[arguments.index("--name") + 1]
            label = arguments[arguments.index("--label") + 1]
            label_name, nonce = label.split("=", 1)
            assert label_name == build.BUILT_IMAGE_SMOKE_LABEL
            image_index = arguments.index(self.world.image_id)
            environment = [
                arguments[index + 1]
                for index, value in enumerate(arguments)
                if value == "--env"
            ]
            self.world.smoke_container = {
                "Id": self.world.smoke_container_id,
                "Name": f"/{name}",
                "Image": self.world.image_id,
                "Config": {
                    "Image": self.world.image_id,
                    "Labels": {build.BUILT_IMAGE_SMOKE_LABEL: nonce},
                    "User": "10001:10001",
                    "Entrypoint": list(build.BUILT_IMAGE_SMOKE_ENTRYPOINT),
                    "Cmd": list(arguments[image_index + 1 :]),
                    "Env": environment,
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": self.world.smoke_readonly_rootfs,
                    "Privileged": False,
                    "AutoRemove": False,
                    "CapAdd": None,
                    "CapDrop": ["ALL"],
                    "Binds": None,
                    "Devices": [],
                    "PidsLimit": 128,
                    "Memory": 536_870_912,
                    "MemorySwap": 536_870_912,
                    "NanoCpus": 1_000_000_000,
                    "SecurityOpt": ["no-new-privileges=true"],
                    "Tmpfs": {
                        "/tmp": "rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700"
                    },
                },
                "State": {
                    "Running": False,
                    "OOMKilled": False,
                    "ExitCode": 0,
                    "Error": "",
                },
                "Mounts": [{"Type": "tmpfs", "Destination": "/tmp"}],
            }
            return build.CommandResult(
                0,
                self.world.smoke_create_stdout,
                b"",
            )
        if arguments[:2] == ("container", "start"):
            assert arguments[2:] == ("--attach", self.world.smoke_container_id)
            assert self.world.smoke_container is not None
            self.world.smoke_container["State"]["ExitCode"] = self.world.smoke_returncode
            return build.CommandResult(
                self.world.smoke_returncode,
                self.world.smoke_stdout,
                b"",
            )
        if arguments[:2] == ("container", "rm"):
            assert arguments[2:] == ("--force", self.world.smoke_container_id)
            assert self.world.smoke_container is not None
            self.world.smoke_container = None
            return build.CommandResult(0, self.world.smoke_container_id.encode("ascii") + b"\n", b"")
        if arguments[:2] == ("image", "rm"):
            assert arguments[2:] == ("--no-prune", LOCAL_TAG)
            assert self.world.tag_present
            self.world.tag_present = False
            return build.CommandResult(0, b"", b"")
        raise AssertionError(command)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = (tmp_path / "repo").resolve()
        self.repo.mkdir(mode=0o700, parents=True)
        self.receipts = (tmp_path / "receipts").resolve()
        self.receipts.mkdir(mode=0o700, parents=True)
        self.output = self.receipts / "build.json"
        self.world = World()
        self.executor = FakeExecutor(self.world)
        # These files are intentionally not build inputs.  They make the
        # worktree differ from the authenticated Git archive fixture.
        (self.repo / "ignored-secret.env").write_text("never-in-context\n", encoding="utf-8")

    def config(self, **overrides: Any) -> build.BuildConfig:
        values: dict[str, Any] = {
            "repo_root": self.repo,
            "receipt_root": self.receipts,
            "source_candidate_sha": CANDIDATE,
            "metadata_envelope_sha": ENVELOPE,
            "local_image_tag": LOCAL_TAG,
            "receipt_path": self.output,
            "execute_local_build": True,
        }
        values.update(overrides)
        return build.BuildConfig(**values)

    def produce(self, config: build.BuildConfig | None = None) -> build.BuildResult:
        return build.produce_build_receipt(
            config or self.config(),
            executor=self.executor,
            now=lambda: dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc),
        )

    def fails(self, code: str, config: build.BuildConfig | None = None) -> None:
        with pytest.raises(build.BuildError) as raised:
            self.produce(config)
        assert raised.value.code == code
        assert str(raised.value) == code
        assert not self.output.exists()
        assert not self.world.tag_present
        assert self.world.smoke_container is None


@pytest.fixture(autouse=True)
def trusted_local_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "_trusted_binary_identity", lambda _path: (1, 2, 3))
    monkeypatch.setattr(build, "_local_docker_socket_identity", lambda: (4, 5, 6))


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_happy_path_builds_only_from_authenticated_archive_and_writes_private_receipt(
    harness: Harness,
) -> None:
    result = harness.produce()
    raw = harness.output.read_bytes()
    assert raw == build._canonical_json_bytes(result.receipt)
    assert result.receipt_sha256 == digest(raw)
    assert stat.S_IMODE(harness.output.stat().st_mode) == 0o600
    assert harness.world.build_input == harness.world.envelope_archive
    assert b"never-in-context" not in harness.world.build_input

    receipt = result.receipt
    assert receipt["schema"] == build.BUILD_RECEIPT_SCHEMA
    assert receipt["source_candidate"] == {
        "tree_sha": CANDIDATE_TREE,
        "archive_sha256": digest(harness.world.candidate_archive),
    }
    assert receipt["metadata_envelope"] == {
        "tree_sha": ENVELOPE_TREE,
        "archive_sha256": digest(harness.world.envelope_archive),
        "changed_paths": [build.RELEASE_MANIFEST_PATH],
    }
    assert receipt["image_reference"] == harness.world.image_id
    assert receipt["image_config_id"] == harness.world.image_id
    assert receipt["local_oci_manifest"]["construction"] == build.OCI_CONSTRUCTION
    assert receipt["local_oci_manifest"]["docker_archive_sha256"] == digest(
        harness.world.docker_archive
    )
    assert [item["digest"] for item in receipt["local_oci_manifest"]["layers"]] == (
        harness.world.diff_ids
    )
    assert receipt["authority"] == {
        "local_only": True,
        "performs_local_docker_build": True,
        "authoritative_for_release_effects": False,
        "public_launch_authority": False,
        "production_ready": False,
    }
    assert receipt["local_build"]["network_mode"] == "none"
    assert receipt["local_build"]["pull"] is False
    assert receipt["local_build"]["docker_daemon_id_sha256"] == digest(
        DAEMON_ID.encode("ascii")
    )
    smoke = receipt["built_image_smoke"]
    assert smoke == {
        "contract_name": build.BUILT_IMAGE_SMOKE_CONTRACT,
        "image_reference": harness.world.image_id,
        "container_id_sha256": digest(harness.world.smoke_container_id.encode("ascii")),
        "script_sha256": digest(build.BUILT_IMAGE_SMOKE_SCRIPT.encode("utf-8")),
        "entrypoint_enforced": True,
        "application_imported": True,
        "asgi_lifespan_completed": True,
        "health_route_registered": True,
        "runtime_mode": "test",
        "storage_backend": "memory",
        "network_mode": "none",
        "root_filesystem": "read_only",
        "capabilities": "none",
        "no_new_privileges": True,
        "pids_limit": 128,
        "memory_limit_bytes": 536_870_912,
        "memory_swap_limit_bytes": 536_870_912,
        "nano_cpus": 1_000_000_000,
        "external_authority": False,
        "production_runtime_proof": False,
    }

    build_call = next(
        call for call in harness.executor.calls if call["argv"][5:7] == ("image", "build")
    )
    arguments = build_call["argv"][5:]
    assert arguments[:7] == (
        "image",
        "build",
        "--network",
        "none",
        "--pull=false",
        "--platform",
        "linux/amd64",
    )
    assert arguments.count("--pull=false") == 1
    assert "--pull" not in arguments
    assert "--pull=true" not in arguments
    assert arguments[-1] == "-"
    assert "--file" in arguments and arguments[arguments.index("--file") + 1] == build.DOCKERFILE_PATH
    assert "--tag" in arguments and arguments[arguments.index("--tag") + 1] == LOCAL_TAG
    assert build_call["input"] == harness.world.envelope_archive
    assert all(call["argv"][0] in build.TRUSTED_COMMAND_BINARIES for call in harness.executor.calls)
    docker_calls = [call for call in harness.executor.calls if call["argv"][0] == build.TRUSTED_DOCKER_BIN]
    assert docker_calls
    assert all(
        call["argv"][3:5] == ("--host", f"unix://{build.LOCAL_DOCKER_SOCKET}")
        for call in docker_calls
    )
    assert not any(call["argv"][5:7] == ("image", "pull") for call in docker_calls)

    create_call = next(
        call
        for call in docker_calls
        if call["argv"][5:7] == ("container", "create")
    )
    create_arguments = create_call["argv"][5:]
    assert "--network" in create_arguments
    assert create_arguments[create_arguments.index("--network") + 1] == "none"
    assert "--read-only" in create_arguments
    assert create_arguments[create_arguments.index("--cap-drop") + 1] == "ALL"
    assert (
        create_arguments[create_arguments.index("--security-opt") + 1]
        == "no-new-privileges=true"
    )
    assert "--privileged" not in create_arguments
    assert "--volume" not in create_arguments
    assert "--mount" not in create_arguments
    assert all(item in create_arguments for item in build.BUILT_IMAGE_SMOKE_ENVIRONMENT)
    assert harness.world.image_id in create_arguments
    image_index = create_arguments.index(harness.world.image_id)
    assert create_arguments[image_index + 1 :] == build.BUILT_IMAGE_SMOKE_COMMAND
    start_call = next(
        call
        for call in docker_calls
        if call["argv"][5:7] == ("container", "start")
    )
    assert start_call["timeout"] == harness.config().docker_smoke_timeout_s
    assert harness.world.smoke_container is None


def test_built_image_smoke_failure_cleans_container_tag_and_receipt(
    harness: Harness,
) -> None:
    harness.world.smoke_returncode = 17

    harness.fails("built_image_smoke_failed")

    assert any(
        call["argv"][5:7] == ("container", "rm")
        for call in harness.executor.calls
    )


def test_built_image_smoke_rejects_wrong_marker_and_relaxed_runtime_policy(
    harness: Harness,
) -> None:
    harness.world.smoke_stdout = b"optimistic pass\n"
    harness.fails("built_image_smoke_output_invalid")

    replacement = Harness(harness.repo.parent / "relaxed-smoke")
    replacement.world.smoke_readonly_rootfs = False
    replacement.fails("built_image_smoke_policy_invalid")


def test_malformed_smoke_create_output_still_cleans_nonce_bound_container(
    harness: Harness,
) -> None:
    harness.world.smoke_create_stdout = b"not-a-container-id\n"

    harness.fails("built_image_smoke_container_id_invalid")

    assert any(
        call["argv"][5:7] == ("container", "rm")
        for call in harness.executor.calls
    )


def test_registry_free_manifest_is_recomputed_from_config_and_uncompressed_layers(
    harness: Harness,
) -> None:
    receipt = harness.produce().receipt
    local = receipt["local_oci_manifest"]
    manifest = {
        "schemaVersion": 2,
        "mediaType": local["media_type"],
        "config": {
            "mediaType": local["config"]["media_type"],
            "digest": local["config"]["digest"],
            "size": local["config"]["size"],
        },
        "layers": [
            {
                "mediaType": item["media_type"],
                "digest": item["digest"],
                "size": item["size"],
            }
            for item in local["layers"]
        ],
    }
    assert receipt["oci_manifest_digest"] == digest(build._canonical_json_document(manifest))
    assert harness.world.image["RepoDigests"] == []
    save_call = next(
        call for call in harness.executor.calls if call["argv"][5:7] == ("image", "save")
    )
    assert save_call["argv"][7] == harness.world.image_id
    assert save_call["stdout_limit"] == harness.config().max_image_archive_bytes


def test_containerd_backed_docker_save_layout_is_verified_fail_closed(
    harness: Harness,
) -> None:
    archive, image_id, diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        containerd_layout=True,
    )
    harness.world.docker_archive = archive

    receipt = harness.produce().receipt

    assert receipt["image_config_id"] == image_id
    assert [item["digest"] for item in receipt["local_oci_manifest"]["layers"]] == diff_ids


def test_containerd_layer_source_metadata_must_match_verified_layer_bytes(
    harness: Harness,
) -> None:
    archive, _image_id, _diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        containerd_layout=True,
        layer_source_size_delta=1,
    )
    harness.world.docker_archive = archive

    harness.fails("docker_archive_layer_source_invalid")


@pytest.mark.parametrize(
    "layer_source_override",
    [
        {"digest": "sha256:" + "9" * 64},
        {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"},
        {"size": True},
        {"urls": ["https://example.invalid/foreign-layer"]},
    ],
)
def test_containerd_layer_sources_reject_unbound_or_foreign_descriptors(
    harness: Harness,
    layer_source_override: dict[str, Any],
) -> None:
    archive, _image_id, _diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        containerd_layout=True,
        layer_source_override=layer_source_override,
    )
    harness.world.docker_archive = archive

    harness.fails("docker_archive_layer_source_invalid")


@pytest.mark.parametrize(
    "profile_override",
    [
        {"containerd_omit_layer_sources": True},
        {"extra_layer_source": True},
    ],
)
def test_containerd_manifest_requires_the_exact_layer_source_key_set(
    harness: Harness,
    profile_override: dict[str, bool],
) -> None:
    archive, _image_id, _diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        containerd_layout=True,
        **profile_override,
    )
    harness.world.docker_archive = archive

    expected = (
        "docker_archive_config_mismatch"
        if profile_override.get("containerd_omit_layer_sources")
        else "docker_archive_layer_source_invalid"
    )
    harness.fails(expected)


@pytest.mark.parametrize(
    ("hybrid_flag", "code"),
    [
        ("containerd_legacy_config_path", "docker_archive_config_mismatch"),
        ("containerd_legacy_layer_paths", "docker_archive_layer_source_invalid"),
    ],
)
def test_containerd_manifest_cannot_mix_legacy_and_content_addressed_profiles(
    harness: Harness,
    hybrid_flag: str,
    code: str,
) -> None:
    archive, _image_id, _diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        containerd_layout=True,
        **{hybrid_flag: True},
    )
    harness.world.docker_archive = archive

    harness.fails(code)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"execute_local_build": False}, "local_build_not_explicitly_authorized"),
        ({"execute_local_build": "true"}, "local_build_not_explicitly_authorized"),
        ({"source_candidate_sha": "short"}, "invalid_source_candidate_sha"),
        ({"metadata_envelope_sha": "f" * 39}, "invalid_metadata_envelope_sha"),
        ({"local_image_tag": "propertyquarry:latest"}, "invalid_local_image_tag"),
        ({"local_image_tag": "propertyquarry-local-candidate:latest"}, "invalid_local_image_tag"),
        ({"docker_build_timeout_s": 7_201}, "invalid_resource_limit"),
        ({"docker_build_timeout_s": True}, "invalid_resource_limit"),
        ({"docker_build_timeout_s": float("nan")}, "invalid_resource_limit"),
        ({"docker_build_timeout_s": float("inf")}, "invalid_resource_limit"),
        ({"max_command_output_bytes": 1.5}, "invalid_resource_limit"),
        ({"max_image_archive_bytes": True}, "invalid_resource_limit"),
    ],
)
def test_invalid_or_nonexplicit_configuration_performs_no_commands(
    harness: Harness,
    overrides: dict[str, Any],
    code: str,
) -> None:
    harness.fails(code, harness.config(**overrides))
    assert harness.executor.calls == []


def test_receipt_must_be_unused_direct_child_of_private_root(
    harness: Harness, tmp_path: Path
) -> None:
    harness.fails(
        "receipt_outside_root",
        harness.config(receipt_path=(tmp_path / "outside.json").resolve()),
    )
    harness.output.write_bytes(b"preexisting")
    with pytest.raises(build.BuildError) as raised:
        harness.produce()
    assert raised.value.code == "receipt_already_exists"
    assert harness.output.read_bytes() == b"preexisting"
    assert harness.executor.calls == []


def test_metadata_envelope_must_descend_and_change_only_allowlisted_paths(
    harness: Harness,
) -> None:
    harness.world.ancestor_returncode = 1
    harness.fails("metadata_envelope_not_descendant")
    assert not any(call["argv"][0] == build.TRUSTED_DOCKER_BIN for call in harness.executor.calls)

    replacement = Harness(harness.repo.parent / "second")
    replacement.world.changed_paths.append("ea/app/substituted.py")
    replacement.fails("metadata_envelope_contains_source_changes")
    assert not any(call["argv"][0] == build.TRUSTED_DOCKER_BIN for call in replacement.executor.calls)


def test_metadata_envelope_cannot_change_dockerfile(harness: Harness) -> None:
    harness.world.envelope_archive = tar_bytes(
        {
            build.DOCKERFILE_PATH: harness.world.dockerfile + b"RUN echo substituted\n",
            build.RELEASE_MANIFEST_PATH: harness.world.release_manifest,
        }
    )
    harness.fails("dockerfile_changed_in_metadata_envelope")


def test_local_git_info_attributes_cannot_rewrite_authenticated_archive(
    harness: Harness,
) -> None:
    attributes = harness.repo / ".git" / "info" / "attributes"
    attributes.parent.mkdir(parents=True)
    attributes.write_text("tracked.txt export-ignore\n", encoding="utf-8")
    harness.fails("git_local_attributes_forbidden")
    assert not any(
        call["argv"][0] == build.TRUSTED_DOCKER_BIN for call in harness.executor.calls
    )


def test_release_manifest_is_bound_to_candidate_and_exact_authority_shape(
    harness: Harness,
) -> None:
    wrong = manifest_bytes("9" * 40)
    harness.world.envelope_archive = tar_bytes(
        {
            build.DOCKERFILE_PATH: harness.world.dockerfile,
            build.RELEASE_MANIFEST_PATH: wrong,
        }
    )
    harness.fails("release_manifest_invalid")


@pytest.mark.parametrize(
    "dockerfile",
    [
        b"FROM python:3.12-slim\n",
        f"FROM {BASE_REFERENCE}\nADD https://example.invalid/payload /tmp/payload\n".encode(),
        f"FROM {BASE_REFERENCE}\nRUN --network=host echo unsafe\n".encode(),
        f"# syntax=docker/dockerfile:1\nFROM {BASE_REFERENCE}\n".encode(),
        f"#syntax=docker/dockerfile:1\nFROM {BASE_REFERENCE}\n".encode(),
        f"#  syntax = docker/dockerfile:1\nFROM {BASE_REFERENCE}\n".encode(),
        f"# ChEcK=skip=JSONArgsRecommended\nFROM {BASE_REFERENCE}\n".encode(),
        f"FROM {BASE_REFERENCE}\nADD\thttps://example.invalid/payload /tmp/payload\n".encode(),
        f"FROM {BASE_REFERENCE}\nCOPY\t--from=remote/image /a /a\n".encode(),
        f"FROM {BASE_REFERENCE}\nRUN --mount=type=secret echo unsafe\n".encode(),
        f"FROM {BASE_REFERENCE}\nRUN\t--mount=type=secret echo unsafe\n".encode(),
    ],
)
def test_dockerfile_requires_digest_pinned_local_only_build_contract(
    harness: Harness, dockerfile: bytes
) -> None:
    harness.world.dockerfile = dockerfile
    harness.world.candidate_archive = tar_bytes(
        {
            build.DOCKERFILE_PATH: dockerfile,
            build.RELEASE_MANIFEST_PATH: b"old\n",
        }
    )
    harness.world.envelope_archive = tar_bytes(
        {
            build.DOCKERFILE_PATH: dockerfile,
            build.RELEASE_MANIFEST_PATH: harness.world.release_manifest,
        }
    )
    expected = (
        "dockerfile_base_not_digest_pinned"
        if b"FROM python:3.12-slim\n" == dockerfile
        else "dockerfile_local_only_contract_invalid"
    )
    harness.fails(expected)


def test_dockerfile_allows_scratch_runtime_copied_from_prior_local_stage() -> None:
    dockerfile = (
        f"FROM {BASE_REFERENCE} AS prepared\n"
        "RUN echo prepared\n"
        "FROM scratch AS runtime\n"
        "COPY --from=prepared / /\n"
    ).encode("ascii")

    assert build._dockerfile_base_references(dockerfile) == (BASE_REFERENCE,)


@pytest.mark.parametrize(
    "dockerfile",
    (
        f"FROM {BASE_REFERENCE} AS prepared\nFROM scratch AS runtime\nCOPY --from=missing / /\n".encode(),
        f"FROM {BASE_REFERENCE} AS prepared\nFROM scratch AS runtime\nCOPY --from=python:3.12-slim / /\n".encode(),
        f"FROM {BASE_REFERENCE} AS prepared\nFROM scratch AS prepared\n".encode(),
    ),
)
def test_dockerfile_rejects_nonlocal_or_ambiguous_multistage_sources(
    dockerfile: bytes,
) -> None:
    with pytest.raises(build.BuildError) as raised:
        build._dockerfile_base_references(dockerfile)

    assert raised.value.code == "dockerfile_local_only_contract_invalid"


@pytest.mark.parametrize(
    "directive",
    (
        "# escape=`",
        "#escape=`",
        "#  EsCaPe = `",
        "#\tESCAPE\t=\t`",
    ),
)
def test_dockerfile_parser_escape_cannot_hide_remote_add(
    harness: Harness, directive: str
) -> None:
    dockerfile = (
        f"{directive}\n"
        f"FROM {BASE_REFERENCE}\n"
        "RUN echo \\\n"
        "ADD https://example.invalid/x /x\n"
    ).encode("ascii")
    harness.world.dockerfile = dockerfile
    harness.world.candidate_archive = tar_bytes(
        {
            build.DOCKERFILE_PATH: dockerfile,
            build.RELEASE_MANIFEST_PATH: b"old\n",
        }
    )
    harness.world.envelope_archive = tar_bytes(
        {
            build.DOCKERFILE_PATH: dockerfile,
            build.RELEASE_MANIFEST_PATH: harness.world.release_manifest,
        }
    )
    harness.fails("dockerfile_local_only_contract_invalid")
    assert not any(
        call["argv"][0] == build.TRUSTED_DOCKER_BIN
        for call in harness.executor.calls
    )


def test_digest_pinned_base_must_already_be_observable_locally(harness: Harness) -> None:
    harness.world.base_image["RepoDigests"] = []
    harness.fails("base_image_digest_unavailable_locally")


def test_base_image_onbuild_triggers_are_forbidden(harness: Harness) -> None:
    harness.world.base_image["Config"]["OnBuild"] = ["ADD https://example.invalid /tmp"]
    harness.fails("base_image_onbuild_forbidden")


def test_build_never_overwrites_an_existing_local_tag(harness: Harness) -> None:
    harness.world.tag_exists = True
    harness.fails("local_image_tag_already_exists")
    assert not any(call["argv"][5:7] == ("image", "build") for call in harness.executor.calls)


def test_prebuild_failure_never_adopts_or_deletes_a_racing_foreign_tag(
    harness: Harness,
) -> None:
    class RacingExecutor(FakeExecutor):
        def run(self, *args: Any, **kwargs: Any) -> build.CommandResult:
            command = tuple(args[0])
            if command[0] == build.TRUSTED_DOCKER_BIN and command[5:] == (
                "image",
                "inspect",
                BASE_REFERENCE,
            ):
                self.calls.append(
                    {
                        "argv": command,
                        "input": kwargs["input_data"],
                        "timeout": kwargs["timeout_s"],
                        "stdout_limit": kwargs["max_stdout_bytes"],
                        "stderr_limit": kwargs["max_stderr_bytes"],
                    }
                )
                self.world.tag_present = True
                return build.CommandResult(1, b"", b"")
            return super().run(*args, **kwargs)

    harness.executor = RacingExecutor(harness.world)
    with pytest.raises(build.BuildError) as raised:
        harness.produce()
    assert raised.value.code == "base_image_inspect_failed"
    assert harness.world.tag_present
    assert not harness.world.built
    assert not harness.output.exists()
    assert not any(
        call["argv"][5:7] == ("image", "rm")
        for call in harness.executor.calls
    )


def test_git_second_observation_detects_object_or_archive_substitution(
    harness: Harness,
) -> None:
    harness.world.git_after["candidate_tree"] = "9" * 40
    harness.fails("git_inputs_changed_during_build")


def test_image_second_observation_detects_rootfs_substitution(harness: Harness) -> None:
    harness.world.image_after = copy.deepcopy(harness.world.image)
    harness.world.image_after["RootFS"]["Layers"][0] = "sha256:" + "9" * 64
    harness.fails("built_image_changed_during_receipt")


def test_base_and_daemon_second_observations_are_bound(harness: Harness) -> None:
    harness.world.base_after = copy.deepcopy(harness.world.base_image)
    harness.world.base_after["Id"] = "sha256:" + "9" * 64
    harness.fails("base_image_changed_during_build")

    replacement = Harness(harness.repo.parent / "daemon-second")
    replacement.world.daemon_ids[1] = "substituted-daemon"
    replacement.fails("docker_daemon_changed_during_build")


def test_command_binary_and_socket_identities_are_rechecked(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_calls: dict[str, int] = {}

    def binary_identity(path: str) -> tuple[int, ...]:
        binary_calls[path] = binary_calls.get(path, 0) + 1
        generation = 9 if path == build.TRUSTED_GIT_BIN and binary_calls[path] >= 3 else 1
        return (generation,)

    monkeypatch.setattr(build, "_trusted_binary_identity", binary_identity)
    harness.fails("command_binary_changed")

    replacement = Harness(harness.repo.parent / "socket-identity")
    monkeypatch.setattr(build, "_trusted_binary_identity", lambda _path: (1,))
    socket_calls = 0

    def socket_identity() -> tuple[int, ...]:
        nonlocal socket_calls
        socket_calls += 1
        return (9,) if socket_calls >= 3 else (1,)

    monkeypatch.setattr(build, "_local_docker_socket_identity", socket_identity)
    replacement.fails("docker_socket_changed")


def test_docker_archive_layer_bytes_must_match_config_diff_ids(harness: Harness) -> None:
    archive, _image_id, _diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        layer_payload_mutation=b"substituted layer",
    )
    harness.world.docker_archive = archive
    harness.fails("docker_archive_layer_digest_mismatch")


def test_docker_archive_rejects_unsafe_members(harness: Harness) -> None:
    archive, _image_id, _diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        unsafe_symlink=True,
    )
    harness.world.docker_archive = archive
    harness.fails("docker_archive_unsafe_entry")


def test_docker_archive_rejects_sparse_and_excessive_member_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        sparse = tarfile.TarInfo("sparse-layer")
        sparse.type = tarfile.GNUTYPE_SPARSE
        sparse.size = 0
        archive.addfile(sparse)
    with pytest.raises(build.BuildError) as raised:
        build._docker_archive_members(output.getvalue())
    assert raised.value.code == "docker_archive_sparse_entry"

    ordinary = tar_bytes({"one": b"1", "two": b"2"})
    monkeypatch.setattr(build, "_MAX_DOCKER_ARCHIVE_MEMBERS", 1)
    with pytest.raises(build.BuildError) as raised:
        build._docker_archive_members(ordinary)
    assert raised.value.code == "docker_archive_member_limit"


def test_docker_archive_config_and_inspect_rootfs_must_match(harness: Harness) -> None:
    harness.world.image["RootFS"]["Layers"][0] = "sha256:" + "9" * 64
    harness.fails("docker_archive_rootfs_mismatch")


def test_docker_config_mutation_blocks_receipt(harness: Harness) -> None:
    harness.world.tamper_docker_config = True
    harness.fails("docker_config_mutated")


def test_partial_docker_config_creation_is_removed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError()))
    with pytest.raises(build.BuildError) as raised:
        build._create_docker_config(harness.receipts)
    assert raised.value.code == "docker_config_create_failed"
    assert not any(path.name.startswith(".pq-build-docker-") for path in harness.receipts.iterdir())


def test_owned_buildx_client_state_is_audited_and_removed(
    harness: Harness,
) -> None:
    docker_config = build._create_docker_config(harness.receipts)
    for relative in build._DOCKER_BUILDX_DIRECTORY_LAYOUT:
        (docker_config / relative).mkdir(mode=0o700, exist_ok=True)
    generated_files = {
        "buildx/.buildNodeID": b"builder-node-id\n",
        "buildx/.lock": b"",
        "buildx/activity/default": b"2026-07-20T09:00:00Z",
        "buildx/refs/default/default/09gncdjwovka2u742clk5jp7g": b"build reference",
    }
    for relative, raw in generated_files.items():
        candidate = docker_config / relative
        candidate.write_bytes(raw)
        candidate.chmod(0o644 if "refs/default/default/" in relative else 0o600)

    build._audit_docker_config(docker_config)
    build._remove_docker_config(docker_config)

    assert not docker_config.exists()


def test_unexpected_or_unsafe_buildx_client_state_is_rejected(
    harness: Harness,
) -> None:
    docker_config = build._create_docker_config(harness.receipts)
    buildx = docker_config / "buildx"
    buildx.mkdir(mode=0o700)
    (buildx / "remote-builder").write_text("untrusted", encoding="utf-8")

    with pytest.raises(build.BuildError) as raised:
        build._audit_docker_config(docker_config)
    assert raised.value.code == "docker_config_mutated"

    (buildx / "remote-builder").unlink()
    buildx.rmdir()
    build._remove_docker_config(docker_config)


def test_receipt_publication_failure_removes_exact_built_tag_and_allows_retry(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(
            build,
            "_atomic_write_receipt",
            lambda *_args: (_ for _ in ()).throw(BuildErrorForTest()),
        )
        harness.fails("receipt_write_failed")
    assert any(
        call["argv"][5:7] == ("image", "rm") for call in harness.executor.calls
    )
    assert not harness.world.tag_present
    harness.executor = FakeExecutor(harness.world)
    result = harness.produce()
    assert result.receipt["image_config_id"] == harness.world.image_id
    assert harness.world.tag_present


class BuildErrorForTest(build.BuildError):
    def __init__(self) -> None:
        super().__init__("receipt_write_failed")


def test_every_baseexception_after_build_cleans_the_exact_tag(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        build,
        "_docker_archive_oci_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        harness.produce()
    assert not harness.world.tag_present
    assert not harness.output.exists()


def test_atomic_receipt_link_interrupt_removes_exact_published_inode(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = build.os.link

    def link_then_interrupt(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(build.os, "link", link_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        build._atomic_write_receipt(harness.output, harness.receipts, b"{}\n")
    assert not harness.output.exists()
    assert not any(path.name.startswith(".build.json.") for path in harness.receipts.iterdir())


def test_injected_executor_cannot_bypass_output_limit(harness: Harness) -> None:
    harness.world.return_oversized_build_output = True
    with pytest.raises(build.BuildError) as raised:
        harness.produce()
    assert raised.value.code == "command_output_limit"
    assert not harness.output.exists()
    # The injected executor returned no valid immutable image ID.  The tag is
    # therefore deliberately left untouched instead of being adopted as ours.
    assert harness.world.tag_present
    assert not any(
        call["argv"][5:7] == ("image", "rm")
        for call in harness.executor.calls
    )


def test_git_archives_reject_path_escape_even_before_docker(harness: Harness) -> None:
    harness.world.candidate_archive = tar_bytes(
        {
            build.DOCKERFILE_PATH: harness.world.dockerfile,
            build.RELEASE_MANIFEST_PATH: b"old\n",
        },
        symlink=("escape", "../../outside"),
    )
    harness.fails("git_archive_unsafe_entry")
    assert not any(call["argv"][0] == build.TRUSTED_DOCKER_BIN for call in harness.executor.calls)


def test_clock_must_be_utc_and_receipt_is_not_published_early(harness: Harness) -> None:
    with pytest.raises(build.BuildError) as raised:
        build.produce_build_receipt(
            harness.config(),
            executor=harness.executor,
            now=lambda: dt.datetime(2026, 7, 18, 12, 0),
        )
    assert raised.value.code == "invalid_clock"
    assert not harness.output.exists()


def test_command_errors_are_redacted_and_do_not_publish_receipt(harness: Harness) -> None:
    class FailingExecutor(FakeExecutor):
        def run(self, *args: Any, **kwargs: Any) -> build.CommandResult:
                result = super().run(*args, **kwargs)
                command = tuple(args[0])
                if command[0] == build.TRUSTED_DOCKER_BIN and command[5:7] == ("image", "build"):
                    self.world.tag_present = False
                    return build.CommandResult(17, b"secret stdout", b"secret stderr")
                return result

    harness.executor = FailingExecutor(harness.world)
    harness.fails("local_docker_build_failed")


def test_parser_requires_explicit_mutation_flag() -> None:
    parser = build._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--repo-root",
                "/tmp/repo",
                "--receipt-root",
                "/tmp/receipts",
                "--source-candidate-sha",
                CANDIDATE,
                "--metadata-envelope-sha",
                ENVELOPE,
                "--local-image-tag",
                LOCAL_TAG,
                "--output-receipt",
                "/tmp/receipts/build.json",
            ]
        )
