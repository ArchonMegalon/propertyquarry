from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import PIL
from fastapi import HTTPException

from app.api.routes import public_tours
from app.product import property_tour_governed_reservations as reservations
from app.product import property_tour_hosting
from scripts import propertyquarry_prater_ai_panorama_closeout as closeout
from scripts import propertyquarry_prater_governed_volume_bootstrap as bootstrap
from scripts import property_tour_governed_reservation as script_reservation


def _marker_bytes(
    *,
    revoked_at: str = "2026-07-24T12:00:00Z",
) -> bytes:
    return reservations.canonical_governed_prater_revocation_bytes(
        {
            "authority": reservations.GOVERNED_PRATER_REVOCATION_AUTHORITY,
            "revocation_id": "a" * 32,
            "revoked_at": revoked_at,
            "schema": reservations.GOVERNED_PRATER_REVOCATION_SCHEMA,
            "slug": reservations.GOVERNED_PRATER_SLUG,
            "status": reservations.GOVERNED_PRATER_REVOCATION_STATUS,
            "tour_sha256": reservations.GOVERNED_PRATER_TOUR_SHA256,
            "version": reservations.GOVERNED_PRATER_REVOCATION_VERSION,
        }
    )


def _attest_source_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
) -> Path:
    source = Path(str(getattr(module, "__file__"))).resolve()
    details = source.stat(follow_symlinks=False)
    monkeypatch.setattr(module, "ENTRYPOINT_PATH", source)
    monkeypatch.setattr(module, "ENTRYPOINT_UID", os.geteuid())
    monkeypatch.setattr(module, "ENTRYPOINT_GID", os.getegid())
    monkeypatch.setattr(module, "ENTRYPOINT_MODE", stat.S_IMODE(details.st_mode))
    monkeypatch.setattr(module, "EXECUTION_UID", os.geteuid())
    monkeypatch.setattr(module, "EXECUTION_GID", os.getegid())
    return source


def test_bootstrap_no_argument_stdout_contract_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    root = tmp_path / "governed"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    monkeypatch.setattr(bootstrap, "GOVERNED_ROOT", root)
    monkeypatch.setattr(bootstrap, "INITIAL_UID", current_uid)
    monkeypatch.setattr(bootstrap, "INITIAL_GID", current_gid)
    monkeypatch.setattr(bootstrap, "RUNTIME_UID", current_uid)
    monkeypatch.setattr(bootstrap, "RUNTIME_GID", current_gid)
    entrypoint = _attest_source_entrypoint(monkeypatch, bootstrap)
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    assert bootstrap.main() == 0
    raw = capsysbinary.readouterr().out
    payload = json.loads(raw.decode("ascii"))
    assert set(payload) == {
        "private_values_redacted",
        "root_device",
        "root_empty",
        "root_gid",
        "root_inode",
        "root_mode",
        "root_uid",
        "schema",
        "status",
        "version",
    }
    assert payload == {
        "private_values_redacted": True,
        "root_device": root.stat().st_dev,
        "root_empty": True,
        "root_gid": current_gid,
        "root_inode": root.stat().st_ino,
        "root_mode": 493,
        "root_uid": current_uid,
        "schema": (
            "propertyquarry.prater-governed-volume-bootstrap-result.v1"
        ),
        "status": "initialized",
        "version": 1,
    }
    assert raw == bootstrap._canonical(payload)
    assert bootstrap.INITIAL_UID == current_uid
    assert bootstrap.ROOT_MODE == 0o755


def test_bootstrap_rejects_nonempty_or_extra_argument_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    root = tmp_path / "governed"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    (root / "unexpected").write_text("x", encoding="ascii")
    monkeypatch.setattr(bootstrap, "GOVERNED_ROOT", root)
    monkeypatch.setattr(bootstrap, "INITIAL_UID", os.geteuid())
    monkeypatch.setattr(bootstrap, "INITIAL_GID", os.getegid())
    entrypoint = _attest_source_entrypoint(monkeypatch, bootstrap)
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])
    assert bootstrap.main() == 1
    assert capsysbinary.readouterr().out == b""

    monkeypatch.setattr(sys, "argv", [str(entrypoint), "argument"])
    assert bootstrap.main() == 2
    assert capsysbinary.readouterr().out == b""


def test_closeout_no_argument_stdout_byte_matches_immutable_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    governed_root = tmp_path / "governed"
    governed_root.mkdir(mode=0o755)
    governed_root.chmod(0o755)
    (governed_root / reservations.GOVERNED_PRATER_SLUG).mkdir()
    request_root = tmp_path / "runtime"
    request_root.mkdir()
    request_path = request_root / "closeout.json"
    raw = _marker_bytes()
    request_path.write_bytes(raw)
    request_path.chmod(0o400)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    monkeypatch.setattr(closeout, "GOVERNED_ROOT", governed_root)
    monkeypatch.setattr(closeout, "REQUEST_PATH", request_path)
    monkeypatch.setattr(closeout, "REQUEST_UID", current_uid)
    monkeypatch.setattr(closeout, "REQUEST_GID", current_gid)
    monkeypatch.setattr(closeout, "ROOT_UID", current_uid)
    monkeypatch.setattr(closeout, "ROOT_GID", current_gid)
    monkeypatch.setattr(closeout, "MARKER_UID", current_uid)
    monkeypatch.setattr(closeout, "MARKER_GID", current_gid)
    entrypoint = _attest_source_entrypoint(monkeypatch, closeout)
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    assert closeout.main() == 0
    assert capsysbinary.readouterr().out == raw
    marker = governed_root / reservations.GOVERNED_PRATER_REVOCATION_FILENAME
    assert marker.read_bytes() == raw
    details = marker.stat(follow_symlinks=False)
    assert stat.S_IMODE(details.st_mode) == 0o444
    assert details.st_nlink == 1
    assert reservations.validate_governed_prater_revocation_bytes(raw)[
        "status"
    ] == "revoked"

    assert closeout.main() == 0
    assert capsysbinary.readouterr().out == raw
    assert marker.read_bytes() == raw


def _create_protected_closeout_target(
    root: Path,
    kind: str,
) -> None:
    target = root / reservations.GOVERNED_PRATER_SLUG
    if kind == "partial-directory":
        target.mkdir()
        (target / "partial.tmp").write_bytes(b"partial")
    elif kind == "regular-file":
        target.write_bytes(b"partial")
    elif kind == "symlink":
        outside = root.parent / "closeout-symlink-target"
        outside.write_bytes(b"outside")
        target.symlink_to(outside)
    elif kind == "broken-symlink":
        target.symlink_to(root / "missing-forever")
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "socket":
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            endpoint.bind(str(target))
        finally:
            endpoint.close()
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "target_kind",
    (
        "partial-directory",
        "regular-file",
        "symlink",
        "broken-symlink",
        "fifo",
        "socket",
    ),
)
def test_closeout_marks_any_no_follow_protected_target_in_isolated_process(
    tmp_path: Path,
    target_kind: str,
) -> None:
    short_root: Path | None = None
    if target_kind == "socket":
        short_root = Path(tempfile.mkdtemp(prefix="pqc-", dir="/tmp"))
    base = short_root if short_root is not None else tmp_path
    governed_root = base / "governed"
    governed_root.mkdir(mode=0o755)
    governed_root.chmod(0o755)
    _create_protected_closeout_target(governed_root, target_kind)
    request_root = base / "runtime"
    request_root.mkdir()
    request_path = request_root / "closeout.json"
    raw = _marker_bytes()
    request_path.write_bytes(raw)
    request_path.chmod(0o400)
    repository_root = Path(__file__).resolve().parents[1]
    code = (
        "import os,sys;"
        "from pathlib import Path;"
        "sys.path.insert(0,sys.argv[1]);"
        "from scripts import propertyquarry_prater_ai_panorama_closeout as c;"
        "c.GOVERNED_ROOT=Path(sys.argv[2]);"
        "c.REQUEST_PATH=Path(sys.argv[3]);"
        "uid=os.geteuid();gid=os.getegid();"
        "c.REQUEST_UID=uid;c.REQUEST_GID=gid;"
        "c.ROOT_UID=uid;c.ROOT_GID=gid;"
        "c.MARKER_UID=uid;c.MARKER_GID=gid;"
        "os.umask(0o777);"
        "os.write(1,c.closeout())"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(repository_root),
            str(governed_root),
            str(request_path),
        ],
        cwd=base,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8",
        errors="replace",
    )
    assert completed.stdout == raw
    assert completed.stderr == b""
    marker = governed_root / reservations.GOVERNED_PRATER_REVOCATION_FILENAME
    assert marker.read_bytes() == raw
    assert os.stat(
        governed_root / reservations.GOVERNED_PRATER_SLUG,
        follow_symlinks=False,
    )
    if short_root is not None:
        shutil.rmtree(short_root)


def test_closeout_detects_protected_target_type_race_after_marker_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    governed_root = tmp_path / "governed"
    governed_root.mkdir(mode=0o755)
    governed_root.chmod(0o755)
    target = governed_root / reservations.GOVERNED_PRATER_SLUG
    target.mkdir()
    request_root = tmp_path / "runtime"
    request_root.mkdir()
    request_path = request_root / "closeout.json"
    raw = _marker_bytes()
    request_path.write_bytes(raw)
    request_path.chmod(0o400)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    monkeypatch.setattr(closeout, "GOVERNED_ROOT", governed_root)
    monkeypatch.setattr(closeout, "REQUEST_PATH", request_path)
    for name, value in (
        ("REQUEST_UID", current_uid),
        ("REQUEST_GID", current_gid),
        ("ROOT_UID", current_uid),
        ("ROOT_GID", current_gid),
        ("MARKER_UID", current_uid),
        ("MARKER_GID", current_gid),
    ):
        monkeypatch.setattr(closeout, name, value)
    monkeypatch.setenv("EA_RUNTIME_MODE", "test")
    monkeypatch.setenv(
        "EA_GOVERNED_PUBLIC_TOUR_DIR",
        str(governed_root),
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_UID",
        current_uid,
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_GID",
        current_gid,
    )
    entrypoint = _attest_source_entrypoint(monkeypatch, closeout)
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])
    original_target_identity = closeout._governed_target_identity
    calls = 0

    def _raced_target_identity(
        root_descriptor: int,
    ) -> os.stat_result:
        nonlocal calls
        observed = original_target_identity(root_descriptor)
        calls += 1
        if calls == 1:
            target.rmdir()
            target.write_bytes(b"replaced")
        return observed

    monkeypatch.setattr(
        closeout,
        "_governed_target_identity",
        _raced_target_identity,
    )

    assert closeout.main() == 1
    assert capsysbinary.readouterr().out == b""
    assert (
        governed_root / reservations.GOVERNED_PRATER_REVOCATION_FILENAME
    ).read_bytes() == raw


@pytest.mark.parametrize(
    "crash_stage",
    ("marker-created", "marker-private"),
)
def test_closeout_resumes_pre_durable_fixed_marker_crash_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_stage: str,
) -> None:
    governed_root = tmp_path / "governed"
    governed_root.mkdir(mode=0o755)
    governed_root.chmod(0o755)
    (governed_root / reservations.GOVERNED_PRATER_SLUG).write_bytes(
        b"partial-private-state"
    )
    request_root = tmp_path / "runtime"
    request_root.mkdir()
    request_path = request_root / "closeout.json"
    raw = _marker_bytes()
    request_path.write_bytes(raw)
    request_path.chmod(0o400)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    monkeypatch.setattr(closeout, "GOVERNED_ROOT", governed_root)
    monkeypatch.setattr(closeout, "REQUEST_PATH", request_path)
    for name, value in (
        ("REQUEST_UID", current_uid),
        ("REQUEST_GID", current_gid),
        ("ROOT_UID", current_uid),
        ("ROOT_GID", current_gid),
        ("MARKER_UID", current_uid),
        ("MARKER_GID", current_gid),
    ):
        monkeypatch.setattr(closeout, name, value)
    monkeypatch.setenv("EA_RUNTIME_MODE", "test")
    monkeypatch.setenv(
        "EA_GOVERNED_PUBLIC_TOUR_DIR",
        str(governed_root),
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_UID",
        current_uid,
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_GID",
        current_gid,
    )
    real_fchmod = os.fchmod
    real_fsync = os.fsync
    injected = False

    def _fchmod_or_crash(descriptor: int, mode: int) -> None:
        nonlocal injected
        if crash_stage == "marker-created" and not injected:
            injected = True
            raise RuntimeError("injected-closeout-crash")
        real_fchmod(descriptor, mode)

    def _fsync_or_crash(descriptor: int) -> None:
        nonlocal injected
        details = os.fstat(descriptor)
        if (
            crash_stage == "marker-private"
            and not injected
            and stat.S_ISREG(details.st_mode)
            and stat.S_IMODE(details.st_mode) == 0o600
        ):
            injected = True
            raise RuntimeError("injected-closeout-crash")
        real_fsync(descriptor)

    old_umask = os.umask(0o777)
    try:
        monkeypatch.setattr(os, "fchmod", _fchmod_or_crash)
        monkeypatch.setattr(os, "fsync", _fsync_or_crash)
        with pytest.raises(RuntimeError, match="injected-closeout-crash"):
            closeout.closeout()
    finally:
        os.umask(old_umask)
        monkeypatch.setattr(os, "fchmod", real_fchmod)
        monkeypatch.setattr(os, "fsync", real_fsync)
    assert injected is True
    marker = governed_root / reservations.GOVERNED_PRATER_REVOCATION_FILENAME
    assert marker.exists()
    if crash_stage == "marker-created":
        assert stat.S_IMODE(marker.stat().st_mode) == 0
    else:
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    with pytest.raises(HTTPException) as unavailable:
        public_tours._load_tour(reservations.GOVERNED_PRATER_SLUG)
    assert unavailable.value.status_code == 503

    marker_inode = marker.stat(follow_symlinks=False).st_ino
    privileged_descriptor = -1
    real_open = os.open
    try:
        if crash_stage == "marker-created" and os.geteuid() != 0:
            marker.chmod(0o600)
            privileged_descriptor = real_open(
                marker,
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            )
            marker.chmod(0)

            def _root_equivalent_open(
                path: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if (
                    path == closeout.MARKER_NAME
                    and not flags & os.O_EXCL
                ):
                    os.lseek(privileged_descriptor, 0, os.SEEK_SET)
                    return os.dup(privileged_descriptor)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            monkeypatch.setattr(os, "open", _root_equivalent_open)
        assert closeout.closeout() == raw
    finally:
        monkeypatch.setattr(os, "open", real_open)
        if privileged_descriptor >= 0:
            os.close(privileged_descriptor)
    assert marker.read_bytes() == raw
    assert stat.S_IMODE(marker.stat().st_mode) == 0o444
    assert marker.stat(follow_symlinks=False).st_ino == marker_inode
    with pytest.raises(HTTPException) as revoked:
        public_tours._load_tour(reservations.GOVERNED_PRATER_SLUG)
    assert revoked.value.status_code == 410


@pytest.mark.parametrize(
    ("failure_checkpoint", "failure_occurrence", "route_status"),
    (
        ("marker-kill-switch-durable", 1, 503),
        ("marker-write-progress", 1, 503),
        ("marker-write-progress", 2, 503),
        ("marker-write-progress", 3, 503),
        ("marker-write-progress", 4, 503),
        ("marker-write-progress", 5, 503),
        ("marker-write-progress", 6, 503),
        ("marker-written", 1, 503),
        ("marker-bytes-fsynced", 1, 503),
        ("marker-readonly", 1, 410),
        ("marker-final-fsynced", 1, 410),
        ("marker-stable-reread", 1, 410),
        ("marker-stable", 1, 410),
    ),
)
def test_closeout_crash_boundaries_resume_to_one_exact_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_checkpoint: str,
    failure_occurrence: int,
    route_status: int,
) -> None:
    governed_root = tmp_path / "governed"
    governed_root.mkdir(mode=0o755)
    governed_root.chmod(0o755)
    target = governed_root / reservations.GOVERNED_PRATER_SLUG
    target.write_bytes(b"partial-private-state")
    request_root = tmp_path / "runtime"
    request_root.mkdir()
    request_path = request_root / "closeout.json"
    raw = _marker_bytes()
    request_path.write_bytes(raw)
    request_path.chmod(0o400)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    monkeypatch.setattr(closeout, "GOVERNED_ROOT", governed_root)
    monkeypatch.setattr(closeout, "REQUEST_PATH", request_path)
    for name, value in (
        ("REQUEST_UID", current_uid),
        ("REQUEST_GID", current_gid),
        ("ROOT_UID", current_uid),
        ("ROOT_GID", current_gid),
        ("MARKER_UID", current_uid),
        ("MARKER_GID", current_gid),
    ):
        monkeypatch.setattr(closeout, name, value)
    monkeypatch.setenv("EA_RUNTIME_MODE", "test")
    monkeypatch.setenv(
        "EA_GOVERNED_PUBLIC_TOUR_DIR",
        str(governed_root),
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_UID",
        current_uid,
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_GID",
        current_gid,
    )
    failed = False
    checkpoint_occurrences = 0

    def _fail_once(name: str) -> None:
        nonlocal checkpoint_occurrences, failed
        if name == failure_checkpoint:
            checkpoint_occurrences += 1
        if (
            name == failure_checkpoint
            and checkpoint_occurrences == failure_occurrence
            and not failed
        ):
            failed = True
            raise RuntimeError("injected-closeout-crash")

    monkeypatch.setattr(closeout, "_checkpoint", _fail_once)
    with pytest.raises(RuntimeError, match="injected-closeout-crash"):
        closeout.closeout()
    assert failed is True
    marker = governed_root / reservations.GOVERNED_PRATER_REVOCATION_FILENAME
    assert marker.exists()
    with pytest.raises(HTTPException) as unavailable:
        public_tours._load_tour(reservations.GOVERNED_PRATER_SLUG)
    assert unavailable.value.status_code == route_status
    assert unavailable.value.detail == (
        "governed_tour_closeout_invalid"
        if route_status == 503
        else "tour_revoked"
    )
    marker_inode = marker.stat(follow_symlinks=False).st_ino

    monkeypatch.setattr(closeout, "_checkpoint", lambda _name: None)
    assert closeout.closeout() == raw
    marker_details = marker.stat(follow_symlinks=False)
    assert marker.read_bytes() == raw
    assert stat.S_IMODE(marker_details.st_mode) == 0o444
    assert marker_details.st_nlink == 1
    assert marker_details.st_ino == marker_inode
    assert set(os.listdir(governed_root)) == {
        reservations.GOVERNED_PRATER_SLUG,
        reservations.GOVERNED_PRATER_REVOCATION_FILENAME,
    }
    with pytest.raises(HTTPException) as revoked:
        public_tours._load_tour(reservations.GOVERNED_PRATER_SLUG)
    assert revoked.value.status_code == 410
    assert revoked.value.detail == "tour_revoked"


def test_closeout_rejects_unknown_inventory_and_never_rewrites_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    governed_root = tmp_path / "governed"
    governed_root.mkdir(mode=0o755)
    governed_root.chmod(0o755)
    (governed_root / reservations.GOVERNED_PRATER_SLUG).mkdir()
    (governed_root / "other-tour").mkdir()
    request_root = tmp_path / "runtime"
    request_root.mkdir()
    request_path = request_root / "closeout.json"
    raw = _marker_bytes()
    request_path.write_bytes(raw)
    request_path.chmod(0o400)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    monkeypatch.setattr(closeout, "GOVERNED_ROOT", governed_root)
    monkeypatch.setattr(closeout, "REQUEST_PATH", request_path)
    for name, value in (
        ("REQUEST_UID", current_uid),
        ("REQUEST_GID", current_gid),
        ("ROOT_UID", current_uid),
        ("ROOT_GID", current_gid),
        ("MARKER_UID", current_uid),
        ("MARKER_GID", current_gid),
    ):
        monkeypatch.setattr(closeout, name, value)
    monkeypatch.setenv("EA_RUNTIME_MODE", "test")
    monkeypatch.setenv(
        "EA_GOVERNED_PUBLIC_TOUR_DIR",
        str(governed_root),
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_UID",
        current_uid,
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_GID",
        current_gid,
    )
    entrypoint = _attest_source_entrypoint(monkeypatch, closeout)
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    crashed = False

    def _crash_with_unrelated_entry(name: str) -> None:
        nonlocal crashed
        if name == "marker-write-progress" and not crashed:
            crashed = True
            raise RuntimeError("injected-closeout-crash")

    monkeypatch.setattr(
        closeout,
        "_checkpoint",
        _crash_with_unrelated_entry,
    )
    assert closeout.main() == 1
    assert capsysbinary.readouterr().out == b""
    marker = governed_root / reservations.GOVERNED_PRATER_REVOCATION_FILENAME
    assert crashed is True
    assert marker.exists()
    with pytest.raises(HTTPException) as unavailable:
        public_tours._load_tour(reservations.GOVERNED_PRATER_SLUG)
    assert unavailable.value.status_code == 503

    monkeypatch.setattr(closeout, "_checkpoint", lambda _name: None)
    assert closeout.main() == 1
    assert capsysbinary.readouterr().out == b""
    assert marker.read_bytes() == raw
    assert stat.S_IMODE(marker.stat().st_mode) == 0o444
    with pytest.raises(HTTPException) as revoked:
        public_tours._load_tour(reservations.GOVERNED_PRATER_SLUG)
    assert revoked.value.status_code == 410

    (governed_root / "other-tour").rmdir()
    marker.unlink()
    marker.write_bytes(b"{}\n")
    marker.chmod(0o444)
    before = marker.read_bytes()
    assert closeout.main() == 1
    assert capsysbinary.readouterr().out == b""
    assert marker.read_bytes() == before


@pytest.mark.parametrize(
    "revoked_at",
    (
        "2026-99-99T99:99:99Z",
        "2026-02-29T12:00:00Z",
        "2026-07-24T12:00:60Z",
    ),
)
def test_closeout_rejects_impossible_utc_timestamp(
    revoked_at: str,
) -> None:
    payload = {
        "authority": reservations.GOVERNED_PRATER_REVOCATION_AUTHORITY,
        "revocation_id": "a" * 32,
        "revoked_at": revoked_at,
        "schema": reservations.GOVERNED_PRATER_REVOCATION_SCHEMA,
        "slug": reservations.GOVERNED_PRATER_SLUG,
        "status": reservations.GOVERNED_PRATER_REVOCATION_STATUS,
        "tour_sha256": reservations.GOVERNED_PRATER_TOUR_SHA256,
        "version": reservations.GOVERNED_PRATER_REVOCATION_VERSION,
    }
    raw = (
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    with pytest.raises(ValueError, match="governed_prater_revocation_invalid"):
        reservations.validate_governed_prater_revocation_bytes(raw)
    with pytest.raises(ValueError):
        closeout._validated_marker_bytes(raw)


def test_frozen_entrypoint_constants_match_shared_contract() -> None:
    assert script_reservation.GOVERNED_PRATER_SLUG == (
        reservations.GOVERNED_PRATER_SLUG
    )
    assert str(bootstrap.ENTRYPOINT_PATH) == (
        "/usr/local/libexec/"
        "propertyquarry-prater-governed-volume-bootstrap-v1.py"
    )
    assert bootstrap.ENTRYPOINT_UID == 0
    assert bootstrap.ENTRYPOINT_GID == 0
    assert bootstrap.ENTRYPOINT_MODE == 0o555
    assert bootstrap.EXECUTION_UID == 0
    assert bootstrap.EXECUTION_GID == 0
    assert str(bootstrap.GOVERNED_ROOT) == (
        reservations.GOVERNED_PUBLIC_TOUR_MOUNT_TARGET
    )
    assert bootstrap.INITIAL_UID == 0
    assert bootstrap.INITIAL_GID == 0
    assert bootstrap.RUNTIME_UID == 10001
    assert bootstrap.RUNTIME_GID == 10001
    assert bootstrap.ROOT_MODE == 0o755
    assert str(closeout.ENTRYPOINT_PATH) == (
        "/usr/local/libexec/"
        "propertyquarry-prater-ai-panorama-closeout-v1.py"
    )
    assert closeout.ENTRYPOINT_UID == 0
    assert closeout.ENTRYPOINT_GID == 0
    assert closeout.ENTRYPOINT_MODE == 0o555
    assert closeout.EXECUTION_UID == 0
    assert closeout.EXECUTION_GID == 0
    assert str(closeout.GOVERNED_ROOT) == (
        reservations.GOVERNED_PUBLIC_TOUR_MOUNT_TARGET
    )
    assert str(closeout.REQUEST_PATH) == (
        reservations.GOVERNED_PRATER_CLOSEOUT_REQUEST_PATH
    )
    assert closeout.MARKER_NAME == (
        reservations.GOVERNED_PRATER_REVOCATION_FILENAME
    )
    assert closeout.MARKER_SCHEMA == (
        reservations.GOVERNED_PRATER_REVOCATION_SCHEMA
    )
    assert closeout.MARKER_SLUG == reservations.GOVERNED_PRATER_SLUG
    assert closeout.MARKER_TOUR_SHA256 == (
        reservations.GOVERNED_PRATER_TOUR_SHA256
    )
    assert closeout.MARKER_MODE == (
        reservations.GOVERNED_PRATER_REVOCATION_MODE
    )


def _stage_runtime_python_layout(
    *,
    repository_root: Path,
    dockerfile_name: str,
    image_root: Path,
) -> set[str]:
    dockerfile = (
        repository_root / "ea" / dockerfile_name
    ).read_text(encoding="utf-8")
    copied_scripts: set[str] = set()
    for line in dockerfile.splitlines():
        if not line.startswith("COPY ") or line.rstrip().endswith("\\"):
            continue
        fields = shlex.split(line)
        copy_fields = [
            field for field in fields[1:] if not field.startswith("--")
        ]
        if len(copy_fields) != 2:
            continue
        source_relpath, destination = copy_fields
        if not destination.startswith("/app/"):
            continue
        source = repository_root / source_relpath
        target = image_root / destination.lstrip("/")
        if source.is_dir() and source_relpath == "ea/app":
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file() and source.suffix == ".py":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if (
            source_relpath.startswith("scripts/")
            and source_relpath.endswith(".py")
        ):
            copied_scripts.add(Path(source_relpath).name)
    return copied_scripts


def _run_isolated_protected_slug_cli(
    *,
    image_root: Path,
    script_name: str,
    arguments: list[str],
) -> None:
    script = image_root / "app" / "scripts" / script_name
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    environment["PROPERTYQUARRY_TOUR_LOCK_DIR"] = str(
        image_root / "runtime-locks"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            *arguments,
        ],
        cwd=image_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 1, (
        f"{script_name} isolated guarded CLI failed\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert completed.stdout == ""
    assert completed.stderr.strip().splitlines()[-1] == (
        "governed_tour_slug_reserved"
    )


def test_standalone_slug_guard_is_closed_and_executed_in_runtime_images(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    protected = reservations.GOVERNED_PRATER_SLUG
    web_root = tmp_path / "web-image"
    render_root = tmp_path / "render-image"
    web_scripts = _stage_runtime_python_layout(
        repository_root=repository_root,
        dockerfile_name="Dockerfile.property-web",
        image_root=web_root,
    )
    render_scripts = _stage_runtime_python_layout(
        repository_root=repository_root,
        dockerfile_name="Dockerfile.property",
        image_root=render_root,
    )
    pillow_source = Path(PIL.__file__).resolve().parent
    for image_root in (web_root, render_root):
        shutil.copytree(
            pillow_source,
            image_root / "app" / "scripts" / "PIL",
            dirs_exist_ok=True,
        )
        shutil.copytree(
            pillow_source.parent / "pillow.libs",
            image_root / "app" / "scripts" / "pillow.libs",
            dirs_exist_ok=True,
        )
    guarded_web_clis = {
        "attach_provider_tour_layer.py": [
            "--slug",
            protected,
            "--provider",
            "matterport",
            "--layer-id",
            "isolated-smoke",
        ],
        "import_3dvista_export.py": ["--slug", protected],
        "import_pano2vr_export.py": ["--slug", protected],
        "import_krpano_walkable_scene.py": ["--slug", protected],
        "import_magicfit_walkthrough.py": [
            "--slug",
            protected,
            "--video-path",
            "/nonexistent/guard-must-win.mp4",
        ],
        "generate_property_reconstruction.py": ["--slug", protected],
    }
    assert {
        "property_tour_governed_reservation.py",
        *guarded_web_clis,
    } <= web_scripts
    assert {
        "property_tour_governed_reservation.py",
        "generate_property_reconstruction.py",
        "property_reconstruction_render_bridge.py",
    } <= render_scripts
    for script_name, arguments in guarded_web_clis.items():
        _run_isolated_protected_slug_cli(
            image_root=web_root,
            script_name=script_name,
            arguments=arguments,
        )
    _run_isolated_protected_slug_cli(
        image_root=render_root,
        script_name="generate_property_reconstruction.py",
        arguments=["--slug", protected],
    )

    bridge_smoke = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys;"
                "import importlib.util;"
                "from pathlib import Path;"
                "script=Path(sys.argv[1]);"
                "spec=importlib.util.spec_from_file_location("
                "'property_reconstruction_render_bridge',script);"
                "assert spec is not None and spec.loader is not None;"
                "module=importlib.util.module_from_spec(spec);"
                "sys.modules[spec.name]=module;"
                "spec.loader.exec_module(module);"
                "assert module._valid_generated_bundle_slug(sys.argv[2])=='';"
                "assert module._valid_generated_bundle_slug('ordinary-tour')=="
                "'ordinary-tour'"
            ),
            str(
                render_root
                / "app"
                / "scripts"
                / "property_reconstruction_render_bridge.py"
            ),
            protected,
        ],
        cwd=render_root,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONHOME", "PYTHONPATH"}
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert bridge_smoke.returncode == 0, bridge_smoke.stderr

    assert script_reservation.require_dynamic_tour_slug(
        "ordinary-tour"
    ) == "ordinary-tour"
    with pytest.raises(RuntimeError, match="governed_tour_slug_reserved"):
        script_reservation.require_dynamic_tour_slug(
            reservations.GOVERNED_PRATER_SLUG
        )


def test_dynamic_hosting_writers_and_revoker_reserve_protected_slug(
    tmp_path: Path,
) -> None:
    protected = reservations.GOVERNED_PRATER_SLUG
    protected_dir = tmp_path / protected
    with pytest.raises(
        RuntimeError,
        match="hosted_property_tour_governed_slug_reserved",
    ):
        property_tour_hosting._write_hosted_property_tour_payload(
            protected_dir,
            {"slug": protected},
        )
    assert not protected_dir.exists()

    ordinary_dir = tmp_path / "ordinary-tour"
    with pytest.raises(
        RuntimeError,
        match="hosted_property_tour_governed_slug_reserved",
    ):
        property_tour_hosting._write_hosted_property_tour_payload(
            ordinary_dir,
            {"slug": protected},
        )
    assert not ordinary_dir.exists()

    proof = property_tour_hosting.persist_hosted_property_tour_browser_render_proof(
        slug=protected,
        provider="3dvista",
        proof={},
        public_roots=[tmp_path],
    )
    assert proof == {
        "status": "governed_tour_reserved",
        "slug": protected,
        "provider": "3dvista",
    }
    revoked = property_tour_hosting.revoke_hosted_property_tour_bundle(
        slug=protected,
        principal_id="owner",
    )
    assert revoked == {
        "status": "governed_tour_reserved",
        "slug": protected,
    }

    property_tour_hosting._write_hosted_property_tour_payload(
        ordinary_dir,
        {"slug": "ordinary-tour", "title": "Ordinary"},
    )
    assert (ordinary_dir / "tour.json").is_file()
