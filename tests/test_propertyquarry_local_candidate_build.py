from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
import stat
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
BASE_DIFF_ID = "sha256:" + "a" * 64
DAEMON_ID = "local-daemon-fixture"
LOCAL_TAG = "propertyquarry-local-candidate:fixture"
REGISTRY_REPOSITORY = "127.0.0.1:5000/propertyquarry/web"
REGISTRY_DIGEST = "sha256:" + "7" * 64
DRIFTED_REGISTRY_DIGEST = "sha256:" + "8" * 64


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
    modern: bool = False,
    layer_source_size_offset: int = 0,
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
    config_path = (
        "blobs/sha256/" + image_id.removeprefix("sha256:")
        if modern
        else image_id.removeprefix("sha256:") + ".json"
    )
    layer_paths = (
        ["blobs/sha256/" + item.removeprefix("sha256:") for item in diff_ids]
        if modern
        else [f"layer-{index}/layer.tar" for index in range(len(layers))]
    )
    record: dict[str, Any] = {
        "Config": config_path,
        "RepoTags": [LOCAL_TAG],
        "Layers": layer_paths,
    }
    if modern:
        record["LayerSources"] = {
            diff_id: {
                "mediaType": build.OCI_LAYER_MEDIA_TYPE,
                "size": len(layer) + layer_source_size_offset,
                "digest": diff_id,
            }
            for diff_id, layer in zip(diff_ids, layers, strict=True)
        }
    manifest = [record]
    files: dict[str, bytes] = {
        "manifest.json": canonical(manifest),
        config_path: config_raw,
    }
    for index, (path, payload) in enumerate(zip(layer_paths, layers, strict=True)):
        files[path] = layer_payload_mutation if index == 0 and layer_payload_mutation else payload
    return (
        tar_bytes(files, symlink=("unsafe", "../../outside") if unsafe_symlink else None),
        image_id,
        diff_ids,
    )


class World:
    def __init__(self) -> None:
        self.dockerfile = (
            f"FROM {BASE_REFERENCE} AS build\n"
            "RUN echo fixture\n"
            f"FROM {BASE_REFERENCE} AS runtime\n"
            "COPY --from=build /tmp/source /tmp/source\n"
        ).encode("ascii")
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
        self.daemon_ids = [DAEMON_ID, DAEMON_ID, DAEMON_ID]
        self.base_after: dict[str, Any] | None = None
        self.image_after: dict[str, Any] | None = None
        self.tag_exists = False
        self.built = False
        self.tag_present = False
        self.build_input: bytes | None = None
        self.tamper_docker_config = False
        self.populate_buildx_config = False
        self.tamper_buildx_config = False
        self.observed_docker_config: Path | None = None
        self.observed_buildx_config: Path | None = None
        self.return_oversized_build_output = False
        self.registry_preexisting = False
        self.registry_pushed = False
        self.registry_pulled = False
        self.registry_tag_drift = False
        self.registry_image_after: dict[str, Any] | None = None
        self.publication_local_tag_present = False
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
            "Architecture": "amd64",
            "Os": "linux",
            "RepoDigests": ["python@" + BASE_DIGEST],
            "RootFS": {"Type": "layers", "Layers": [BASE_DIFF_ID]},
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

    @property
    def registry_tag_name(self) -> str:
        return build._registry_candidate_tag(CANDIDATE, self.image_id)

    @property
    def registry_candidate_reference(self) -> str:
        return f"{REGISTRY_REPOSITORY}:{self.registry_tag_name}"

    @property
    def registry_immutable_reference(self) -> str:
        return f"{REGISTRY_REPOSITORY}@{REGISTRY_DIGEST}"

    def registry_image(self) -> dict[str, Any]:
        value = copy.deepcopy(
            self.registry_image_after
            if self.registry_image_after is not None
            else self.image
        )
        value["RepoTags"] = (
            [LOCAL_TAG, self.registry_candidate_reference]
            if self.publication_local_tag_present
            else [LOCAL_TAG]
        )
        value["RepoDigests"] = [self.registry_immutable_reference]
        return value


class FakeRegistryObserver:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, str, float]] = []
        self.tag_calls = 0
        self.override: object | None = None

    def manifest_digest(
        self,
        repository: str,
        reference: str,
        *,
        timeout_s: float,
    ) -> str | None:
        self.calls.append((repository, reference, timeout_s))
        assert repository == REGISTRY_REPOSITORY
        if isinstance(self.override, BaseException):
            raise self.override
        if self.override is not None:
            return self.override  # type: ignore[return-value]
        if reference == self.world.registry_tag_name:
            self.tag_calls += 1
            drift_at = 2 if self.world.registry_preexisting else 3
            if self.world.registry_tag_drift and self.tag_calls >= drift_at:
                return DRIFTED_REGISTRY_DIGEST
            if self.world.registry_preexisting or self.world.registry_pushed:
                return REGISTRY_DIGEST
            return None
        if reference == REGISTRY_DIGEST:
            return REGISTRY_DIGEST
        raise AssertionError(reference)


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
        self.world.observed_docker_config = docker_config
        assert command[3:5] == ("--host", f"unix://{build.LOCAL_DOCKER_SOCKET}")
        arguments = command[5:]
        buildx_candidates: list[Path] = []
        if arguments[:2] == ("image", "build"):
            buildx_candidates = [
                path
                for path in docker_config.parent.iterdir()
                if build._BUILDX_CONFIG_NAME_RE.fullmatch(path.name)
            ]
            assert len(buildx_candidates) == 1
            self.world.observed_buildx_config = buildx_candidates[0]
        if self.world.tamper_docker_config and arguments[:2] == ("image", "build"):
            (docker_config / "config.json").write_text('{"auths":{}}\n', encoding="utf-8")
        if self.world.populate_buildx_config and arguments[:2] == ("image", "build"):
            buildx_config = buildx_candidates[0]
            refs = buildx_config / "refs" / "default" / "default"
            refs.mkdir(mode=0o700, parents=True)
            (refs / "operation-id").write_text("fixture\n", encoding="utf-8")
            lock = buildx_config / ".lock"
            lock.write_text("", encoding="utf-8")
            lock.chmod(0o600)
        if self.world.tamper_buildx_config and arguments[:2] == ("image", "build"):
            (buildx_candidates[0] / "escape").symlink_to("/etc")
        if arguments[:2] == ("system", "info"):
            value = self.world.daemon_ids[self.daemon_calls]
            self.daemon_calls += 1
            return build.CommandResult(0, canonical(value), b"")
        if arguments[:2] == ("image", "inspect"):
            reference = arguments[2]
            if reference == LOCAL_TAG and not self.world.tag_present:
                return build.CommandResult(0 if self.world.tag_exists else 1, b"", b"")
            if reference == self.world.registry_candidate_reference:
                if not self.world.publication_local_tag_present:
                    return build.CommandResult(1, b"", b"")
                return build.CommandResult(
                    0, canonical([self.world.registry_image()]), b""
                )
            if reference == self.world.registry_immutable_reference:
                assert self.world.registry_pulled
                return build.CommandResult(
                    0, canonical([self.world.registry_image()]), b""
                )
            if reference == BASE_REFERENCE:
                value = self.world.base_image
                if self.base_calls > 0 and self.world.base_after is not None:
                    value = self.world.base_after
                self.base_calls += 1
                return build.CommandResult(0, canonical([value]), b"")
            if reference == BASE_IMAGE_ID:
                value = self.world.base_image
                if self.world.base_after is not None:
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
        if arguments[:2] == ("image", "tag"):
            assert arguments[2:] == (
                self.world.image_id,
                self.world.registry_candidate_reference,
            )
            self.world.publication_local_tag_present = True
            return build.CommandResult(0, b"", b"")
        if arguments[:2] == ("image", "push"):
            assert arguments[2:] == (
                "--quiet",
                self.world.registry_candidate_reference,
            )
            assert self.world.publication_local_tag_present
            self.world.registry_pushed = True
            return build.CommandResult(0, b"", b"")
        if arguments[:2] == ("image", "pull"):
            assert arguments[2:] == (
                "--platform",
                "linux/amd64",
                "--quiet",
                self.world.registry_immutable_reference,
            )
            self.world.registry_pulled = True
            return build.CommandResult(0, b"", b"")
        if arguments[:2] == ("image", "rm"):
            reference = arguments[3]
            assert arguments[2] == "--no-prune"
            if reference == LOCAL_TAG:
                assert self.world.tag_present
                self.world.tag_present = False
            elif reference == self.world.registry_candidate_reference:
                assert self.world.publication_local_tag_present
                self.world.publication_local_tag_present = False
            else:
                raise AssertionError(command)
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
        self.registry_observer = FakeRegistryObserver(self.world)
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
            registry_observer=self.registry_observer,
            now=lambda: dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc),
        )

    def fails(self, code: str, config: build.BuildConfig | None = None) -> None:
        with pytest.raises(build.BuildError) as raised:
            self.produce(config)
        assert raised.value.code == code
        assert str(raised.value) == code
        assert not self.output.exists()
        assert not self.world.tag_present
        assert not self.world.publication_local_tag_present


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
    assert harness.registry_observer.calls == []


def test_render_profile_selects_authenticated_render_dockerfile(
    harness: Harness,
) -> None:
    harness.world.candidate_archive = tar_bytes(
        {
            build.RENDER_DOCKERFILE_PATH: harness.world.dockerfile,
            build.RELEASE_MANIFEST_PATH: b"old metadata manifest\n",
            "tracked.txt": b"candidate\n",
        }
    )
    harness.world.envelope_archive = tar_bytes(
        {
            build.RENDER_DOCKERFILE_PATH: harness.world.dockerfile,
            build.RELEASE_MANIFEST_PATH: harness.world.release_manifest,
            "tracked.txt": b"candidate\n",
        }
    )

    receipt = harness.produce(harness.config(image_kind="render")).receipt

    assert receipt["dockerfile"] == {
        "path": build.RENDER_DOCKERFILE_PATH,
        "sha256": digest(harness.world.dockerfile),
    }
    build_call = next(
        call for call in harness.executor.calls if call["argv"][5:7] == ("image", "build")
    )
    arguments = build_call["argv"][5:]
    assert arguments[arguments.index("--file") + 1] == build.RENDER_DOCKERFILE_PATH
    assert build_call["input"] == harness.world.envelope_archive


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


def test_explicit_loopback_publication_pushes_pulls_and_binds_immutable_identity(
    harness: Harness,
) -> None:
    result = harness.produce(
        harness.config(
            execute_registry_publication=True,
            registry_repository=REGISTRY_REPOSITORY,
        )
    )
    receipt = result.receipt
    publication = receipt["registry_publication"]
    assert receipt["schema"] == build.PUBLISHED_BUILD_RECEIPT_SCHEMA
    assert receipt["image_reference"] == harness.world.registry_immutable_reference
    assert receipt["image_config_id"] == harness.world.image_id
    assert publication == {
        "schema": build.REGISTRY_PUBLICATION_SCHEMA,
        "transport": "loopback_http",
        "repository": REGISTRY_REPOSITORY,
        "candidate_tag": harness.world.registry_candidate_reference,
        "tag_preexisting": False,
        "push_performed": True,
        "manifest_digest": REGISTRY_DIGEST,
        "immutable_reference": harness.world.registry_immutable_reference,
        "pulled_image": {
            "image_config_id": harness.world.image_id,
            "rootfs_diff_ids": harness.world.diff_ids,
            "labels": harness.world.labels,
            "repo_tags": sorted(
                [LOCAL_TAG, harness.world.registry_candidate_reference]
            ),
            "repo_digests": [harness.world.registry_immutable_reference],
        },
        "platform": "linux/amd64",
    }
    assert receipt["authority"] == {
        "local_only": False,
        "performs_local_docker_build": True,
        "performs_registry_publication": True,
        "authoritative_for_release_effects": False,
        "public_launch_authority": False,
        "production_ready": False,
    }
    assert [reference for _repository, reference, _timeout in harness.registry_observer.calls] == [
        harness.world.registry_tag_name,
        harness.world.registry_tag_name,
        harness.world.registry_tag_name,
        REGISTRY_DIGEST,
    ]
    docker_arguments = [
        call["argv"][5:]
        for call in harness.executor.calls
        if call["argv"][0] == build.TRUSTED_DOCKER_BIN
    ]
    assert (
        "image",
        "push",
        "--quiet",
        harness.world.registry_candidate_reference,
    ) in docker_arguments
    assert (
        "image",
        "pull",
        "--platform",
        "linux/amd64",
        "--quiet",
        harness.world.registry_immutable_reference,
    ) in docker_arguments
    assert harness.world.tag_present
    assert harness.world.publication_local_tag_present


def test_exact_preexisting_candidate_publication_is_reused_without_overwrite(
    harness: Harness,
) -> None:
    harness.world.registry_preexisting = True
    receipt = harness.produce(
        harness.config(
            execute_registry_publication=True,
            registry_repository=REGISTRY_REPOSITORY,
        )
    ).receipt
    publication = receipt["registry_publication"]
    assert publication["tag_preexisting"] is True
    assert publication["push_performed"] is False
    assert harness.world.registry_pulled
    assert not harness.world.registry_pushed
    assert not harness.world.publication_local_tag_present
    assert not any(
        call["argv"][5:7] in {("image", "tag"), ("image", "push")}
        for call in harness.executor.calls
    )


def test_preexisting_registry_tag_with_different_image_is_never_overwritten(
    harness: Harness,
) -> None:
    harness.world.registry_preexisting = True
    harness.world.registry_image_after = copy.deepcopy(harness.world.image)
    harness.world.registry_image_after["Id"] = "sha256:" + "9" * 64
    harness.fails(
        "registry_image_config_mismatch",
        harness.config(
            execute_registry_publication=True,
            registry_repository=REGISTRY_REPOSITORY,
        ),
    )
    assert not harness.world.registry_pushed
    assert not any(
        call["argv"][5:7] == ("image", "push")
        for call in harness.executor.calls
    )


def test_registry_tag_drift_or_invalid_observation_fails_closed(harness: Harness) -> None:
    harness.world.registry_tag_drift = True
    harness.fails(
        "registry_tag_changed_during_publication",
        harness.config(
            execute_registry_publication=True,
            registry_repository=REGISTRY_REPOSITORY,
        ),
    )

    replacement = Harness(harness.repo.parent / "invalid-registry-response")
    replacement.registry_observer.override = "sha256:not-a-digest"
    replacement.fails(
        "registry_response_invalid",
        replacement.config(
            execute_registry_publication=True,
            registry_repository=REGISTRY_REPOSITORY,
        ),
    )

    errored = Harness(harness.repo.parent / "registry-error")
    errored.registry_observer.override = RuntimeError("secret registry failure")
    errored.fails(
        "registry_observation_failed",
        errored.config(
            execute_registry_publication=True,
            registry_repository=REGISTRY_REPOSITORY,
        ),
    )


def test_publication_receipt_failure_retries_from_exact_remote_digest(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = harness.config(
        execute_registry_publication=True,
        registry_repository=REGISTRY_REPOSITORY,
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            build,
            "_atomic_write_receipt",
            lambda *_args: (_ for _ in ()).throw(BuildErrorForTest()),
        )
        harness.fails("receipt_write_failed", config)
    assert harness.world.registry_pushed
    assert not harness.world.tag_present
    assert not harness.world.publication_local_tag_present

    harness.executor = FakeExecutor(harness.world)
    harness.registry_observer = FakeRegistryObserver(harness.world)
    retry = harness.produce(config).receipt["registry_publication"]
    assert retry["tag_preexisting"] is True
    assert retry["push_performed"] is False
    assert not any(
        call["argv"][5:7] == ("image", "push")
        for call in harness.executor.calls
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"execute_local_build": False}, "local_build_not_explicitly_authorized"),
        ({"execute_local_build": "true"}, "local_build_not_explicitly_authorized"),
        ({"source_candidate_sha": "short"}, "invalid_source_candidate_sha"),
        ({"metadata_envelope_sha": "f" * 39}, "invalid_metadata_envelope_sha"),
        ({"local_image_tag": "propertyquarry:latest"}, "invalid_local_image_tag"),
        ({"local_image_tag": "propertyquarry-local-candidate:latest"}, "invalid_local_image_tag"),
        ({"image_kind": "unknown"}, "invalid_image_kind"),
        ({"image_kind": True}, "invalid_image_kind"),
        (
            {"execute_registry_publication": True},
            "registry_repository_required",
        ),
        (
            {"registry_repository": REGISTRY_REPOSITORY},
            "registry_publication_not_explicitly_authorized",
        ),
        (
            {
                "execute_registry_publication": "true",
                "registry_repository": REGISTRY_REPOSITORY,
            },
            "registry_publication_not_explicitly_authorized",
        ),
        (
            {
                "execute_registry_publication": True,
                "registry_repository": "registry.example.com/propertyquarry/web",
            },
            "registry_repository_invalid",
        ),
        (
            {
                "execute_registry_publication": True,
                "registry_repository": "localhost:5000/propertyquarry/web",
            },
            "registry_repository_invalid",
        ),
        (
            {
                "execute_registry_publication": True,
                "registry_repository": "127.0.0.1:65536/propertyquarry/web",
            },
            "registry_repository_invalid",
        ),
        (
            {
                "execute_registry_publication": True,
                "registry_repository": "127.0.0.1:5000/propertyquarry/web:latest",
            },
            "registry_repository_invalid",
        ),
        ({"docker_build_timeout_s": 7_201}, "invalid_resource_limit"),
        ({"registry_observe_timeout_s": 121}, "invalid_resource_limit"),
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
        f"FROM {BASE_REFERENCE}\nCOPY --from=future /a /a\n".encode(),
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


def test_dockerfile_allows_only_previously_declared_local_copy_stages() -> None:
    safe = (
        f"FROM {BASE_REFERENCE} AS build\n"
        "RUN echo build\n"
        f"FROM {BASE_REFERENCE} AS runtime\n"
        "COPY --from=build /a /a\n"
        "COPY --from=BUILD /b /b\n"
    ).encode("ascii")
    assert build._dockerfile_base_references(safe) == (BASE_REFERENCE,)

    for unsafe in (
        (
            f"FROM {BASE_REFERENCE} AS build\n"
            f"FROM {BASE_REFERENCE} AS build\n"
        ),
        (
            f"FROM {BASE_REFERENCE} AS runtime\n"
            "COPY --from=0 /a /a\n"
        ),
        (
            f"FROM {BASE_REFERENCE} AS runtime\n"
            "COPY --from=example.invalid/external:tag /a /a\n"
        ),
    ):
        with pytest.raises(build.BuildError) as raised:
            build._dockerfile_base_references(unsafe.encode("ascii"))
        assert raised.value.code == "dockerfile_local_only_contract_invalid"


def test_live_web_dockerfile_satisfies_authenticated_multistage_contract() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1] / build.DOCKERFILE_PATH
    ).read_bytes()
    references = build._dockerfile_base_references(dockerfile)
    assert len(references) == 2
    assert references[0].startswith("python:3.12-slim@sha256:")
    assert references[1].startswith("gcr.io/distroless/cc-debian13@sha256:")


def test_live_render_dockerfile_satisfies_authenticated_scratch_contract() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1] / build.RENDER_DOCKERFILE_PATH
    ).read_bytes()
    references = build._dockerfile_base_references(dockerfile)
    assert len(references) == 1
    assert references[0].startswith("python:3.12-alpine@sha256:")


def test_loopback_registry_observer_uses_bounded_direct_v2_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Headers:
        def __init__(self, values: dict[str, list[str]]) -> None:
            self.values = values

        def get_all(self, name: str) -> list[str] | None:
            return self.values.get(name)

    class Response:
        def __init__(self, status: int, values: dict[str, list[str]]) -> None:
            self.status = status
            self.headers = Headers(values)
            self.closed = False

        def read(self, maximum: int) -> bytes:
            assert maximum == 1
            return b""

        def close(self) -> None:
            self.closed = True

    responses = [
        Response(
            404,
            {"Docker-Distribution-Api-Version": ["registry/2.0"]},
        ),
        Response(
            200,
            {
                "Docker-Distribution-Api-Version": ["registry/2.0"],
                "Content-Type": ["application/vnd.oci.image.manifest.v1+json"],
                "Docker-Content-Digest": [REGISTRY_DIGEST],
            },
        ),
    ]
    connections: list[Any] = []

    class Connection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout
            self.requests: list[tuple[str, str, dict[str, str]]] = []
            self.response = responses[len(connections)]
            self.closed = False
            connections.append(self)

        def request(
            self, method: str, path: str, *, headers: dict[str, str]
        ) -> None:
            self.requests.append((method, path, headers))

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(build.http.client, "HTTPConnection", Connection)
    observer = build.LoopbackRegistryObserver()
    tag = "candidate-" + CANDIDATE + "-" + "a" * 64
    assert (
        observer.manifest_digest(
            REGISTRY_REPOSITORY,
            tag,
            timeout_s=7.5,
        )
        is None
    )
    assert (
        observer.manifest_digest(
            REGISTRY_REPOSITORY,
            REGISTRY_DIGEST,
            timeout_s=7.5,
        )
        == REGISTRY_DIGEST
    )
    assert all(connection.host == "127.0.0.1" for connection in connections)
    assert all(connection.port == 5000 for connection in connections)
    assert all(connection.timeout == 7.5 for connection in connections)
    assert connections[0].requests == [
        (
            "HEAD",
            f"/v2/propertyquarry/web/manifests/{tag}",
            {"Accept": build._REGISTRY_ACCEPT},
        )
    ]
    assert connections[1].requests[0][1].endswith("/manifests/" + REGISTRY_DIGEST)
    assert all(connection.closed for connection in connections)
    assert all(response.closed for response in responses)


@pytest.mark.parametrize(
    ("status", "headers", "body", "code"),
    [
        (
            302,
            {"Docker-Distribution-Api-Version": ["registry/2.0"]},
            b"",
            "registry_observation_failed",
        ),
        (
            200,
            {
                "Content-Type": ["application/vnd.oci.image.manifest.v1+json"],
                "Docker-Content-Digest": [REGISTRY_DIGEST],
            },
            b"",
            "registry_response_invalid",
        ),
        (
            200,
            {
                "Docker-Distribution-Api-Version": ["registry/2.0"],
                "Content-Type": ["text/plain"],
                "Docker-Content-Digest": [REGISTRY_DIGEST],
            },
            b"",
            "registry_response_invalid",
        ),
        (
            200,
            {
                "Docker-Distribution-Api-Version": ["registry/2.0"],
                "Content-Type": ["application/vnd.oci.image.manifest.v1+json"],
                "Docker-Content-Digest": [REGISTRY_DIGEST, DRIFTED_REGISTRY_DIGEST],
            },
            b"",
            "registry_response_invalid",
        ),
        (
            200,
            {
                "Docker-Distribution-Api-Version": ["registry/2.0"],
                "Content-Type": ["application/vnd.oci.image.manifest.v1+json"],
                "Docker-Content-Digest": [REGISTRY_DIGEST],
            },
            b"x",
            "registry_response_invalid",
        ),
    ],
)
def test_loopback_registry_observer_rejects_redirects_and_ambiguous_responses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, list[str]],
    body: bytes,
    code: str,
) -> None:
    class Headers:
        def get_all(self, name: str) -> list[str] | None:
            return headers.get(name)

    class Response:
        def __init__(self) -> None:
            self.status = status
            self.headers = Headers()

        def read(self, _maximum: int) -> bytes:
            return body

        def close(self) -> None:
            return None

    class Connection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(build.http.client, "HTTPConnection", Connection)
    with pytest.raises(build.BuildError) as raised:
        build.LoopbackRegistryObserver().manifest_digest(
            REGISTRY_REPOSITORY,
            REGISTRY_DIGEST,
            timeout_s=1.0,
        )
    assert raised.value.code == code


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


def test_base_digest_alias_may_evaporate_but_content_identity_may_not(
    harness: Harness,
) -> None:
    harness.world.base_after = copy.deepcopy(harness.world.base_image)
    harness.world.base_after["RepoDigests"] = []
    receipt = harness.produce().receipt
    assert receipt["base_images"][0]["observed_repo_digest"] == (
        "python@" + BASE_DIGEST
    )
    assert receipt["base_images"][0]["rootfs_diff_ids"] == [BASE_DIFF_ID]

    changed = Harness(harness.repo.parent / "changed-base-rootfs")
    changed.world.base_after = copy.deepcopy(changed.world.base_image)
    changed.world.base_after["RootFS"]["Layers"][0] = "sha256:" + "b" * 64
    changed.fails("base_image_changed_during_build")


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


def test_modern_docker_archive_layer_sources_are_fully_authenticated(
    harness: Harness,
) -> None:
    archive, image_id, diff_ids = docker_archive(
        layers=harness.world.layers,
        labels=harness.world.labels,
        modern=True,
    )
    harness.world.docker_archive = archive
    harness.world.image_id = image_id
    harness.world.diff_ids = diff_ids
    harness.world.image["Id"] = image_id
    harness.world.image["RootFS"]["Layers"] = diff_ids
    receipt = harness.produce().receipt
    assert receipt["image_config_id"] == image_id
    assert receipt["local_oci_manifest"]["docker_archive_sha256"] == digest(archive)

    changed = Harness(harness.repo.parent / "modern-layer-source-changed")
    archive, image_id, diff_ids = docker_archive(
        layers=changed.world.layers,
        labels=changed.world.labels,
        modern=True,
        layer_source_size_offset=1,
    )
    changed.world.docker_archive = archive
    changed.world.image_id = image_id
    changed.world.diff_ids = diff_ids
    changed.world.image["Id"] = image_id
    changed.world.image["RootFS"]["Layers"] = diff_ids
    changed.fails("docker_archive_layer_sources_invalid")


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


def test_buildx_metadata_is_private_bounded_and_separate_from_credentials(
    harness: Harness,
) -> None:
    harness.world.populate_buildx_config = True
    harness.produce()
    assert harness.world.observed_docker_config is not None
    assert harness.world.observed_buildx_config is not None
    assert not harness.world.observed_docker_config.exists()
    assert not harness.world.observed_buildx_config.exists()

    replacement = Harness(harness.repo.parent / "tampered-buildx")
    replacement.world.tamper_buildx_config = True
    replacement.fails("buildx_config_mutated")


def test_partial_docker_config_creation_is_removed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError()))
    with pytest.raises(build.BuildError) as raised:
        build._create_docker_config(harness.receipts)
    assert raised.value.code == "docker_config_create_failed"
    assert not any(path.name.startswith(".pq-build-docker-") for path in harness.receipts.iterdir())


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
