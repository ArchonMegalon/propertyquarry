from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.verify_propertyquarry_security_bootstrap_attestation import (
    AttestationError,
    capture_authenticated_archive,
    capture_json_stream,
    safe_extract_artifact_archive,
    verify_security_bootstrap_attestation,
)


REPOSITORY = "ArchonMegalon/propertyquarry"
WORKFLOW_REF = f"{REPOSITORY}/.github/workflows/smoke-runtime.yml@refs/heads/main"
BOOTSTRAP_PATH = ".github/workflows/propertyquarry-security-runner-bootstrap.yml"
RUN_ID = "8001"
RUN_ATTEMPT = "2"
JOB_ID = "7001"
BOOTSTRAP_RUN_ID = "9001"
BOOTSTRAP_RUN_ATTEMPT = "1"
HEAD_SHA = "a" * 40
RUNNER_LABEL = "pqsec-" + "b" * 32
RUNNER_NAME = f"pq-security-{BOOTSTRAP_RUN_ID}-{RUN_ID}"
TOKEN_EXPIRES_AT = "2030-01-01T01:00:00Z"
WEB_IMAGE = (
    "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:"
    + "c" * 64
)
RENDER_IMAGE = (
    "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:"
    + "d" * 64
)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_zip(
    path: Path,
    members: list[tuple[str | zipfile.ZipInfo, bytes]],
) -> Path:
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            if isinstance(name, str):
                member = zipfile.ZipInfo(name)
                member.create_system = 3
                if name.endswith("/"):
                    member.external_attr = (stat.S_IFDIR | 0o700) << 16
                    member.external_attr |= 0x10
                else:
                    member.external_attr = (stat.S_IFREG | 0o600) << 16
                member.compress_type = zipfile.ZIP_DEFLATED
            else:
                member = name
            archive.writestr(member, payload)
    return path


def _target() -> dict[str, str]:
    return {
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "job_id": JOB_ID,
        "head_sha": HEAD_SHA,
    }


def _build_bundle(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bootstrap = {
        "schema": "propertyquarry.security_runner_bootstrap.v1",
        "status": "listener_exited",
        "message": "listener reached immutable boundary",
        "repository": REPOSITORY,
        "target": _target(),
        "bootstrap": {
            "run_id": BOOTSTRAP_RUN_ID,
            "run_attempt": BOOTSTRAP_RUN_ATTEMPT,
        },
        "runner": {
            "name": RUNNER_NAME,
            "label": RUNNER_LABEL,
            "agent_id": "6001",
            "listener_exit_code": "2",
            "registration_token_expires_at": TOKEN_EXPIRES_AT,
        },
        "images": {"web": WEB_IMAGE, "render": RENDER_IMAGE},
        "recorded_at": "2030-01-01T00:30:00Z",
    }
    consumption = {
        "schema": "propertyquarry.security_runner_consumption.v1",
        "status": "pass",
        "repository": REPOSITORY,
        "workflow_ref": WORKFLOW_REF,
        "target": _target(),
        "bootstrap": {
            "run_id": BOOTSTRAP_RUN_ID,
            "run_attempt": BOOTSTRAP_RUN_ATTEMPT,
        },
        "runner": {"name": RUNNER_NAME, "label": RUNNER_LABEL},
        "images": {"web": WEB_IMAGE, "render": RENDER_IMAGE},
        "registration_token_expires_at": TOKEN_EXPIRES_AT,
        "verified_at": "2030-01-01T00:31:00Z",
    }
    post_job = {
        "schema": "propertyquarry.security_runner_post_job_integrity.v1",
        "status": "pass",
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "job_id": JOB_ID,
        "runner_name": RUNNER_NAME,
        "runner_label": RUNNER_LABEL,
        "checked_at": "2030-01-01T00:30:30Z",
    }
    preflight_name = f"preflight-{RUN_ID}-{RUN_ATTEMPT}.json"
    preflight = {
        "schema": "propertyquarry.security_runner_preflight.v1",
        "status": "pass",
        "repository": REPOSITORY,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "head_sha": HEAD_SHA,
        "runner_name": RUNNER_NAME,
        "checked_at": "2030-01-01T00:10:00Z",
    }
    payloads = {
        "bootstrap-receipt.json": bootstrap,
        "consumption.json": consumption,
        "post-job-integrity.json": post_job,
        preflight_name: preflight,
    }
    for name, payload in payloads.items():
        _write_json(bundle / name, payload)
    manifest = {
        "schema": "propertyquarry.security_runner_receipt_manifest.v1",
        "status": "pass",
        "repository": REPOSITORY,
        "workflow_ref": WORKFLOW_REF,
        "target": _target(),
        "bootstrap": {
            "run_id": BOOTSTRAP_RUN_ID,
            "run_attempt": BOOTSTRAP_RUN_ATTEMPT,
            "head_sha": HEAD_SHA,
        },
        "runner": {"name": RUNNER_NAME, "label": RUNNER_LABEL},
        "images": {"web": WEB_IMAGE, "render": RENDER_IMAGE},
        "registration_token_expires_at": TOKEN_EXPIRES_AT,
        "files": {name: _sha256(bundle / name) for name in payloads},
        "recorded_at": "2030-01-01T00:32:00Z",
    }
    _write_json(bundle / "receipt-manifest.json", manifest)
    metadata = {
        "schema": "propertyquarry.security_bootstrap_artifact_metadata.v1",
        "selection_count": 1,
        "artifact": {
            "id": "5001",
            "name": (
                "propertyquarry-security-runner-bootstrap-target-"
                f"{RUN_ID}-{RUN_ATTEMPT}-{JOB_ID}"
            ),
            "digest": "sha256:" + "e" * 64,
            "expired": False,
            "workflow_run_id": BOOTSTRAP_RUN_ID,
        },
        "bootstrap_workflow_run": {
            "id": BOOTSTRAP_RUN_ID,
            "run_attempt": BOOTSTRAP_RUN_ATTEMPT,
            "repository": REPOSITORY,
            "event": "workflow_dispatch",
            "path": BOOTSTRAP_PATH,
            "head_branch": "main",
            "head_sha": HEAD_SHA,
            "status": "completed",
            "conclusion": "success",
        },
        "observed_at": "2030-01-01T00:33:00Z",
    }
    metadata_path = tmp_path / "artifact-metadata.json"
    _write_json(metadata_path, metadata)
    return bundle, metadata_path


def _verify(bundle: Path, metadata_path: Path) -> dict[str, object]:
    return verify_security_bootstrap_attestation(
        artifact_root=bundle,
        artifact_metadata_path=metadata_path,
        expected_repository=REPOSITORY,
        expected_workflow_ref=WORKFLOW_REF,
        expected_bootstrap_workflow_path=BOOTSTRAP_PATH,
        expected_run_id=RUN_ID,
        expected_run_attempt=RUN_ATTEMPT,
        expected_job_id=JOB_ID,
        expected_head_sha=HEAD_SHA,
        expected_runner_label=RUNNER_LABEL,
        expected_token_expires_at=TOKEN_EXPIRES_AT,
        expected_web_image=WEB_IMAGE,
        expected_render_image=RENDER_IMAGE,
        verified_at=datetime(2030, 1, 1, 0, 34, tzinfo=timezone.utc),
    )


def test_exact_bootstrap_bundle_issues_same_run_attestation(tmp_path: Path) -> None:
    bundle, metadata_path = _build_bundle(tmp_path)

    result = _verify(bundle, metadata_path)

    assert result["status"] == "pass"
    assert result["target"] == _target()
    assert result["runner"] == {"name": RUNNER_NAME, "label": RUNNER_LABEL}
    assert result["artifact"] == {
        "id": "5001",
        "name": (
            "propertyquarry-security-runner-bootstrap-target-"
            f"{RUN_ID}-{RUN_ATTEMPT}-{JOB_ID}"
        ),
        "digest": "sha256:" + "e" * 64,
        "workflow_run_id": BOOTSTRAP_RUN_ID,
    }


def test_rejects_cross_target_replay_even_with_rehashed_manifest(tmp_path: Path) -> None:
    bundle, metadata_path = _build_bundle(tmp_path)
    consumption_path = bundle / "consumption.json"
    consumption = json.loads(consumption_path.read_text(encoding="utf-8"))
    consumption["target"]["run_id"] = "8002"
    _write_json(consumption_path, consumption)
    manifest_path = bundle / "receipt-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["consumption.json"] = _sha256(consumption_path)
    _write_json(manifest_path, manifest)

    with pytest.raises(AttestationError, match="consumption receipt.target.run_id mismatch"):
        _verify(bundle, metadata_path)


def test_rejects_receipt_mutation_against_digest_manifest(tmp_path: Path) -> None:
    bundle, metadata_path = _build_bundle(tmp_path)
    preflight = bundle / f"preflight-{RUN_ID}-{RUN_ATTEMPT}.json"
    preflight.write_text(preflight.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(AttestationError, match="receipt digest mismatch"):
        _verify(bundle, metadata_path)


def test_rejects_bootstrap_workflow_path_substitution(tmp_path: Path) -> None:
    bundle, metadata_path = _build_bundle(tmp_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["bootstrap_workflow_run"]["path"] = ".github/workflows/other.yml"
    _write_json(metadata_path, metadata)

    with pytest.raises(
        AttestationError, match="bootstrap_workflow_run.path mismatch"
    ):
        _verify(bundle, metadata_path)


@pytest.mark.parametrize(
    "unsafe_expiry",
    [
        "2030-01-01T01:00:00+00:00",
        "2030-01-01T01:00:00.000Z",
        "2030-01-01T01:00:00z",
        "2030-02-30T01:00:00Z",
    ],
)
def test_rejects_noncanonical_or_invalid_token_expiry(
    tmp_path: Path, unsafe_expiry: str
) -> None:
    bundle, metadata_path = _build_bundle(tmp_path)

    with pytest.raises(AttestationError, match="canonical UTC RFC3339"):
        verify_security_bootstrap_attestation(
            artifact_root=bundle,
            artifact_metadata_path=metadata_path,
            expected_repository=REPOSITORY,
            expected_workflow_ref=WORKFLOW_REF,
            expected_bootstrap_workflow_path=BOOTSTRAP_PATH,
            expected_run_id=RUN_ID,
            expected_run_attempt=RUN_ATTEMPT,
            expected_job_id=JOB_ID,
            expected_head_sha=HEAD_SHA,
            expected_runner_label=RUNNER_LABEL,
            expected_token_expires_at=unsafe_expiry,
            expected_web_image=WEB_IMAGE,
            expected_render_image=RENDER_IMAGE,
        )


def test_capture_authenticated_archive_rejects_digest_mismatch_without_output(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "artifact.zip"

    with pytest.raises(AttestationError, match="digest mismatch"):
        capture_authenticated_archive(
            io.BytesIO(b"not-the-authenticated-archive"),
            destination=destination,
            expected_digest="sha256:" + "0" * 64,
            maximum_bytes=1024,
        )

    assert not destination.exists()


def test_capture_json_rejects_duplicate_keys_without_output(tmp_path: Path) -> None:
    destination = tmp_path / "api.json"

    with pytest.raises(AttestationError, match="duplicate JSON key"):
        capture_json_stream(
            io.BytesIO(b'{"artifact":1,"artifact":2}'),
            destination=destination,
            maximum_bytes=1024,
        )

    assert not destination.exists()


def test_safe_extractor_accepts_bounded_regular_files(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "artifact.zip",
        [
            ("receipt.json", b"{}\n"),
            ("nested/", b""),
            ("nested/evidence.json", b'{"ok":true}\n'),
        ],
    )
    destination = tmp_path / "bundle"

    receipt = safe_extract_artifact_archive(
        archive,
        destination=destination,
        maximum_archive_bytes=4096,
        maximum_entries=4,
        maximum_member_bytes=128,
        maximum_uncompressed_bytes=256,
    )

    assert receipt["entries"] == 3
    assert (destination / "receipt.json").read_bytes() == b"{}\n"
    assert (destination / "nested" / "evidence.json").read_bytes() == b'{"ok":true}\n'


@pytest.mark.parametrize("member_name", ["../escape.json", "/absolute.json"])
def test_safe_extractor_rejects_traversal_and_absolute_members(
    tmp_path: Path, member_name: str
) -> None:
    archive = _write_zip(tmp_path / "artifact.zip", [(member_name, b"unsafe")])
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="archive member path"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()


def test_safe_extractor_rejects_symlink_member(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("receipt-link.json")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _write_zip(tmp_path / "artifact.zip", [(link, b"receipt.json")])
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="symlink is forbidden"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()


def test_safe_extractor_rejects_special_member(tmp_path: Path) -> None:
    fifo = zipfile.ZipInfo("receipt-pipe")
    fifo.create_system = 3
    fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
    archive = _write_zip(tmp_path / "artifact.zip", [(fifo, b"")])
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="special file or link is forbidden"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    "member_type",
    [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK],
)
def test_safe_extractor_rejects_slash_terminated_link_and_special_modes(
    tmp_path: Path, member_type: int
) -> None:
    member = zipfile.ZipInfo("unsupported-entry/")
    member.create_system = 3
    member.external_attr = (member_type | 0o600) << 16
    archive = _write_zip(tmp_path / "artifact.zip", [(member, b"")])
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="forbidden"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    ("member_name", "member_type"),
    [
        ("regular-named-as-directory/", stat.S_IFREG),
        ("directory-without-slash", stat.S_IFDIR),
    ],
)
def test_safe_extractor_rejects_directory_metadata_name_disagreement(
    tmp_path: Path, member_name: str, member_type: int
) -> None:
    member = zipfile.ZipInfo(member_name)
    member.create_system = 3
    member.external_attr = (member_type | 0o700) << 16
    archive = _write_zip(tmp_path / "artifact.zip", [(member, b"")])
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="metadata and name disagree"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    ("create_system", "external_attr"),
    [(3, 0o600 << 16), (0, (stat.S_IFREG | 0o600) << 16)],
)
def test_safe_extractor_rejects_unknown_mode_metadata(
    tmp_path: Path, create_system: int, external_attr: int
) -> None:
    member = zipfile.ZipInfo("unknown-mode-entry")
    member.create_system = create_system
    member.external_attr = external_attr
    archive = _write_zip(tmp_path / "artifact.zip", [(member, b"")])
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="unknown"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()


def test_safe_extractor_rejects_hardlink_archive(tmp_path: Path) -> None:
    archive = tmp_path / "artifact-with-hardlink.tar"
    with tarfile.open(archive, mode="w") as bundle:
        regular = tarfile.TarInfo("receipt.json")
        regular.size = 3
        bundle.addfile(regular, io.BytesIO(b"{}\n"))
        hardlink = tarfile.TarInfo("receipt-hardlink.json")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "receipt.json"
        bundle.addfile(hardlink)
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="not a valid ZIP"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()


def test_safe_extractor_rejects_hardlinked_archive_inode(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "artifact.zip", [("receipt.json", b"{}\n")])
    hardlink = tmp_path / "artifact-hardlink.zip"
    os.link(archive, hardlink)
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="must not be hardlinked"):
        safe_extract_artifact_archive(hardlink, destination=destination)

    assert not destination.exists()


def test_safe_extractor_rejects_oversized_member(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "artifact.zip", [("large.json", b"x" * 65)])
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="member exceeds"):
        safe_extract_artifact_archive(
            archive,
            destination=destination,
            maximum_member_bytes=64,
        )

    assert not destination.exists()


def test_safe_extractor_rejects_aggregate_size_overage(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "artifact.zip",
        [("one.json", b"a" * 40), ("two.json", b"b" * 40)],
    )
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="aggregate uncompressed size"):
        safe_extract_artifact_archive(
            archive,
            destination=destination,
            maximum_member_bytes=64,
            maximum_uncompressed_bytes=64,
        )

    assert not destination.exists()


def test_safe_extractor_rejects_entry_count_overage(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "artifact.zip",
        [("one.json", b"1"), ("two.json", b"2")],
    )
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="entry count"):
        safe_extract_artifact_archive(
            archive,
            destination=destination,
            maximum_entries=1,
        )

    assert not destination.exists()


def test_safe_extractor_rejects_duplicate_member_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "artifact.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        archive = _write_zip(
            archive_path,
            [("duplicate.json", b"first"), ("duplicate.json", b"second")],
        )
    destination = tmp_path / "bundle"

    with pytest.raises(AttestationError, match="duplicate archive member"):
        safe_extract_artifact_archive(archive, destination=destination)

    assert not destination.exists()
