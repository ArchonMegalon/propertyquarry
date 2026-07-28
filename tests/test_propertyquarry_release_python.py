from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest

from scripts import propertyquarry_release_python_create_root as root_creator
from scripts import propertyquarry_release_python_verify as verifier


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_python_pin_binds_interpreter_lock_and_private_environment() -> None:
    pin = json.loads(
        (
            ROOT / "config" / "propertyquarry_release_python_pin.json"
        ).read_text(encoding="utf-8")
    )

    assert pin["schema"] == "propertyquarry.release-python-pin.v3"
    assert pin["python_binary"] == "/usr/bin/python3.12"
    assert _sha256(Path(pin["python_binary"])) == pin["python_binary_sha256"]
    assert pin["python_stdlib"] == "/usr/lib/python3.12"
    stdlib_tree = verifier._tree_digest(
        Path(pin["python_stdlib"]),
        owners={0},
        allowed_symlinks=verifier.STDLIB_ALLOWED_SYMLINKS,
        forbid_bytecode=False,
        label="test source standard library",
    )
    assert stdlib_tree == (
        pin["python_stdlib_tree_sha256"],
        pin["python_stdlib_entry_count"],
        pin["python_stdlib_regular_bytes"],
    )
    sitecustomize = Path(pin["python_sitecustomize"])
    assert sitecustomize == verifier.SOURCE_SITECUSTOMIZE
    assert _sha256(sitecustomize) == pin["python_sitecustomize_sha256"]
    assert sitecustomize.stat().st_mode & 0o7777 == pin["python_sitecustomize_mode"]
    libpython_link = Path(pin["python_libpython_link"])
    assert libpython_link == verifier.SOURCE_LIBPYTHON_LINK
    assert os.readlink(libpython_link) == pin["python_libpython_link_target"]
    libpython = Path(pin["python_libpython"])
    assert libpython == verifier.SOURCE_LIBPYTHON
    assert _sha256(libpython) == pin["python_libpython_sha256"]
    assert libpython.stat().st_mode & 0o7777 == pin["python_libpython_mode"]
    requirements_input = ROOT / pin["requirements_input"]
    assert _sha256(requirements_input) == pin["requirements_input_sha256"]
    base_lock = ROOT / pin["requirements_base_lock"]
    assert _sha256(base_lock) == pin["requirements_base_lock_sha256"]
    lock = ROOT / pin["requirements_lock"]
    assert _sha256(lock) == pin["requirements_lock_sha256"]
    assert pin["venv"] == ".propertyquarry_release_tools/release-venv"
    assert len(pin["venv_tree_sha256"]) == 64
    assert pin["venv_entry_count"] > 0
    assert pin["venv_regular_bytes"] > 0


def test_release_python_shell_pins_match_the_authenticated_pin_and_lock() -> None:
    pin = json.loads(
        (
            ROOT / "config" / "propertyquarry_release_python_pin.json"
        ).read_text(encoding="utf-8")
    )
    bootstrap = (
        ROOT / "scripts" / "bootstrap_propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")
    launcher = (
        ROOT / "scripts" / "propertyquarry_release_python.sh"
    ).read_text(encoding="utf-8")

    def assignment(source: str, name: str) -> str:
        match = re.search(
            rf"^{re.escape(name)}=([0-9a-f]{{64}})$",
            source,
            re.MULTILINE,
        )
        assert match is not None
        return match.group(1)

    requirements_input = ROOT / str(pin["requirements_input"])
    assert (
        f'INPUT="${{ROOT}}/{pin["requirements_input"]}"'
        in bootstrap
    )
    assert assignment(bootstrap, "INPUT_SHA256") == (
        pin["requirements_input_sha256"]
    )
    assert assignment(bootstrap, "INPUT_SHA256") == _sha256(requirements_input)
    lock = ROOT / str(pin["requirements_lock"])
    assert (
        f'LOCK="${{ROOT}}/{pin["requirements_lock"]}"'
        in bootstrap
    )
    assert assignment(bootstrap, "LOCK_SHA256") == pin["requirements_lock_sha256"]
    assert assignment(bootstrap, "LOCK_SHA256") == _sha256(lock)
    assert assignment(bootstrap, "SYSTEM_PYTHON_SHA256") == (
        pin["python_binary_sha256"]
    )
    assert assignment(launcher, "SYSTEM_PYTHON_SHA256") == (
        pin["python_binary_sha256"]
    )
    for required_format in (
        "color",
        "date-time",
        "duration",
        "hostname",
        "idn-hostname",
        "iri",
        "iri-reference",
        "json-pointer",
        "relative-json-pointer",
        "time",
        "uri",
        "uri-reference",
        "uri-template",
    ):
        assert f'"{required_format}"' in launcher
    assert 'version("jsonschema") != "4.26.0"' in launcher
    assert 'version("pip-audit") != "2.10.1"' in launcher
    assert "format_checker.conforms(value, format_name)" in launcher
    probe_match = re.search(
        r'''-I -B -c '\n(?P<source>.*?)\n'\n\)"''',
        launcher,
        flags=re.DOTALL,
    )
    assert probe_match is not None
    ast.parse(probe_match.group("source"))


def test_release_requirements_parity_is_coherent() -> None:
    requirements_input = (
        ROOT / "config" / "propertyquarry_release_verifier_requirements.in"
    ).read_bytes()
    requirements_lock = (
        ROOT / "config" / "propertyquarry_release_verifier_requirements.lock"
    ).read_bytes()
    base_lock = (ROOT / "ea" / "requirements.lock").read_bytes()

    issues = verifier.requirements_lock_issues(
        requirements_input,
        base_lock,
        requirements_lock,
    )

    assert issues == []


def test_release_requirements_parity_normalizes_names_and_checks_versions() -> None:
    issues = verifier.requirements_lock_issues(
        b"-r ../ea/requirements.lock\n"
        b"Example_Package[extra-one,extra_two]==1.2.3\n",
        b"Base_Dependency==2.0\n",
        b"example-package==1.2.3 \\\n"
        b"    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        b"base-dependency==2.0 \\\n"
        b"    --hash=sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        b"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n",
    )
    mismatch = verifier.requirements_lock_issues(
        b"-r ../ea/requirements.lock\n"
        b"example.package==1.2.3\n",
        b"base-dependency==2.0\n",
        b"example-package==1.2.4 \\\n"
        b"    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        b"base-dependency==2.0 \\\n"
        b"    --hash=sha256:ffffffffffffffffffffffffffffffff"
        b"ffffffffffffffffffffffffffffffff\n",
    )
    base_mismatch = verifier.requirements_lock_issues(
        b"-r ../ea/requirements.lock\n"
        b"example.package==1.2.3\n",
        b"base-dependency==2.1\n",
        b"example-package==1.2.3 \\\n"
        b"    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        b"base-dependency==2.0 \\\n"
        b"    --hash=sha256:ffffffffffffffffffffffffffffffff"
        b"ffffffffffffffffffffffffffffffff\n",
    )

    assert issues == []
    assert mismatch == [
        "example.package==1.2.3 resolves to example-package==1.2.4 in the "
        "compiled requirements lock"
    ]
    assert base_mismatch == [
        "base requirement base-dependency==2.1 resolves to "
        "base-dependency==2.0 in the compiled requirements lock"
    ]


def test_release_requirements_parity_rejects_unsupported_input_syntax() -> None:
    issues = verifier.requirements_lock_issues(
        b"example>=1\n",
        b"base-dependency==2.0\n",
        b"example==1.0 \\\n"
        b"    --hash=sha256:cccccccccccccccccccccccccccccccc"
        b"cccccccccccccccccccccccccccccccc\n"
        b"base-dependency==2.0 \\\n"
        b"    --hash=sha256:abababababababababababababababab"
        b"abababababababababababababababab\n",
    )

    assert issues == [
        "release verifier requirements input line 1 is not an exact direct "
        "pin or requirement include",
        "release verifier requirements input has no direct pins",
        "release verifier requirements input is missing the canonical "
        "-r ../ea/requirements.lock include",
    ]


def test_release_requirements_parity_rejects_truncated_lock_hashes() -> None:
    issues = verifier.requirements_lock_issues(
        b"-r ../ea/requirements.lock\nexample==1.0\n",
        b"base-dependency==2.0\n",
        b"example==1.0 \\\n"
        b"    --hash=sha256:dddddddddddddddddddddddddddddddd"
        b"dddddddddddddddddddddddddddddddd \\\n"
        b"base-dependency==2.0 \\\n"
        b"    --hash=sha256:bcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbc"
        b"bcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbc\n",
    )

    assert issues == [
        "example==1.0 has an unterminated hash continuation in the compiled "
        "requirements lock"
    ]


def test_release_requirements_parity_entrypoint_is_early_and_concise() -> None:
    expected_issues = verifier.requirements_lock_issues(
        (
            ROOT
            / "config"
            / "propertyquarry_release_verifier_requirements.in"
        ).read_bytes(),
        (ROOT / "ea" / "requirements.lock").read_bytes(),
        (
            ROOT
            / "config"
            / "propertyquarry_release_verifier_requirements.lock"
        ).read_bytes(),
    )
    result = subprocess.run(
        [
            "/usr/bin/python3.12",
            "-I",
            "-S",
            "-B",
            str(
                ROOT
                / "scripts"
                / "propertyquarry_release_python_verify.py"
            ),
            "--check-requirements-parity",
        ],
        cwd=ROOT,
        env={
            "HOME": "/tmp",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.stdout == ""
    if expected_issues:
        assert result.returncode == 2
        assert result.stderr == (
            "error: release Python verification failed: release verifier "
            "requirements input/lock is stale: "
            "jsonschema[format-nongpl]==4.26.0 is missing from the compiled "
            "requirements lock\n"
        )
    else:
        assert result.returncode == 0
        assert result.stderr == ""


def test_release_python_root_creator_makes_exact_private_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release-venv"

    identity = root_creator._create_and_identify(target)

    metadata = target.stat()
    assert identity == f"{metadata.st_dev}:{metadata.st_ino}"
    assert metadata.st_mode & 0o7777 == 0o700


def test_release_python_root_creator_rejects_existing_or_linked_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release-venv"
    target.mkdir(mode=0o700)
    with pytest.raises(root_creator.CreationError, match="already exists"):
        root_creator._create_and_identify(target)

    target.rmdir()
    target.symlink_to(tmp_path)
    with pytest.raises(root_creator.CreationError, match="already exists"):
        root_creator._create_and_identify(target)


@pytest.mark.parametrize("sent_signal", (signal.SIGHUP, signal.SIGINT, signal.SIGTERM))
def test_release_python_root_creator_entrypoint_blocks_signal_until_identity(
    tmp_path: Path,
    sent_signal: signal.Signals,
) -> None:
    target = tmp_path / "release-venv"
    marker = tmp_path / "opened"
    code = f"""
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, {str(ROOT)!r})
from scripts import propertyquarry_release_python_create_root as creator

creator.TARGET = Path({str(target)!r})
original_open = creator.os.open
delayed = False

def delayed_open(path, flags, *args, **kwargs):
    global delayed
    if not delayed:
        delayed = True
        Path({str(marker)!r}).write_text("ready\\n", encoding="utf-8")
        time.sleep(0.25)
    return original_open(path, flags, *args, **kwargs)

creator.os.open = delayed_open
raise SystemExit(creator._entrypoint())
"""
    process = subprocess.Popen(
        ["/usr/bin/python3.12", "-I", "-S", "-B", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 3
    while not marker.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("root creator did not reach the identity window")
        time.sleep(0.01)
    os.killpg(process.pid, sent_signal)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    assert stderr == ""
    metadata = target.stat()
    assert stdout == f"{metadata.st_dev}:{metadata.st_ino}\n"
    assert metadata.st_mode & 0o7777 == 0o700


def test_release_python_root_creator_quarantines_broken_identity_pipe(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release-venv"
    code = f"""
from pathlib import Path
import sys

sys.path.insert(0, {str(ROOT)!r})
from scripts import propertyquarry_release_python_create_root as creator

creator.TARGET = Path({str(target)!r})
raise SystemExit(creator._entrypoint())
"""
    read_descriptor, write_descriptor = os.pipe()
    os.close(read_descriptor)
    try:
        result = subprocess.run(
            ["/usr/bin/python3.12", "-I", "-S", "-B", "-c", code],
            check=False,
            stdout=write_descriptor,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.close(write_descriptor)

    assert result.returncode == 2
    assert result.stdout is None
    assert "identity could not be reported" in result.stderr
    assert "Broken pipe" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr
    assert not target.exists()
    quarantines = list(tmp_path.glob("release-venv.incomplete.*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_dir()
    assert quarantines[0].stat().st_mode & 0o7777 == 0o700
    assert f"retained at {quarantines[0]}" in result.stderr


def test_release_python_root_creator_neutralizes_broken_diagnostic_pipe(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release-venv"
    target.mkdir(mode=0o700)
    code = f"""
from pathlib import Path
import sys

sys.path.insert(0, {str(ROOT)!r})
from scripts import propertyquarry_release_python_create_root as creator

creator.TARGET = Path({str(target)!r})
raise SystemExit(creator._entrypoint())
"""
    read_descriptor, write_descriptor = os.pipe()
    os.close(read_descriptor)
    try:
        result = subprocess.run(
            ["/usr/bin/python3.12", "-I", "-S", "-B", "-c", code],
            check=False,
            stdout=subprocess.PIPE,
            stderr=write_descriptor,
            text=True,
        )
    finally:
        os.close(write_descriptor)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr is None
    assert target.is_dir()
    assert list(tmp_path.glob("release-venv.incomplete.*")) == []


def test_release_verifier_tree_digest_detects_content_tampering(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release-venv"
    root.mkdir(mode=0o700)
    payload = root / "module.py"
    payload.write_text("trusted = True\n", encoding="utf-8")
    payload.chmod(0o600)

    before = verifier._tree_digest(root)
    payload.write_text("trusted = None\n", encoding="utf-8")
    after = verifier._tree_digest(root)

    assert before != after
    assert before[1:] == after[1:]


def test_release_verifier_tree_digest_rejects_peer_writable_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release-venv"
    root.mkdir(mode=0o700)
    payload = root / "module.py"
    payload.write_text("trusted = True\n", encoding="utf-8")
    payload.chmod(0o666)

    with pytest.raises(SystemExit, match="peer-writable"):
        verifier._tree_digest(root)


def test_release_verifier_tree_digest_rejects_unexpected_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release-venv"
    root.mkdir(mode=0o700)
    os.symlink("elsewhere", root / "unexpected")

    with pytest.raises(SystemExit, match="symlink is forbidden"):
        verifier._tree_digest(root)


def test_release_verifier_tree_digest_allows_only_canonical_lib64_link(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release-venv"
    root.mkdir(mode=0o700)
    (root / "lib").mkdir(mode=0o700)
    os.symlink("lib", root / "lib64")

    digest, entries, regular_bytes = verifier._tree_digest(root)

    assert len(digest) == 64
    assert entries == 3
    assert regular_bytes == 0


def test_release_verifier_tree_digest_commits_directory_nodes_and_modes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release-venv"
    root.mkdir(mode=0o700)

    before = verifier._tree_digest(root)
    empty_directory = root / "namespace"
    empty_directory.mkdir(mode=0o700)
    with_directory = verifier._tree_digest(root)
    empty_directory.chmod(0o750)
    with_mode_change = verifier._tree_digest(root)

    assert before[1:] == (1, 0)
    assert with_directory[1:] == (2, 0)
    assert before[0] != with_directory[0]
    assert with_directory[0] != with_mode_change[0]


def test_release_verifier_tree_digest_commits_executable_mode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release-venv"
    root.mkdir(mode=0o700)
    tool = root / "tool"
    tool.write_bytes(b"#!/bin/true\n")
    tool.chmod(0o700)

    executable = verifier._tree_digest(root)
    tool.chmod(0o600)
    non_executable = verifier._tree_digest(root)

    assert executable[0] != non_executable[0]
    assert executable[1:] == non_executable[1:]


def test_release_verifier_runtime_tree_detects_file_and_directory_tampering(
    tmp_path: Path,
) -> None:
    root = tmp_path / "python3.12"
    root.mkdir(mode=0o700)
    module = root / "runtime.py"
    module.write_text("trusted = True\n", encoding="utf-8")
    module.chmod(0o600)
    bytecode_directory = root / "__pycache__"
    bytecode_directory.mkdir(mode=0o700)
    bytecode = bytecode_directory / "runtime.cpython-312.pyc"
    bytecode.write_bytes(b"pinned-bytecode")
    bytecode.chmod(0o600)

    before = verifier._tree_digest(
        root,
        owners={os.geteuid()},
        allowed_symlinks={},
        forbid_bytecode=False,
        label="test source standard library",
    )
    module.write_text("trusted = None\n", encoding="utf-8")
    after_file_change = verifier._tree_digest(
        root,
        owners={os.geteuid()},
        allowed_symlinks={},
        forbid_bytecode=False,
        label="test source standard library",
    )
    (root / "injected_namespace").mkdir(mode=0o700)
    after_directory_change = verifier._tree_digest(
        root,
        owners={os.geteuid()},
        allowed_symlinks={},
        forbid_bytecode=False,
        label="test source standard library",
    )

    assert before[0] != after_file_change[0]
    assert after_file_change[0] != after_directory_change[0]
    assert before[1] + 1 == after_directory_change[1]


def test_release_verifier_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    anchor.mkdir(mode=0o700)
    real = anchor / "real"
    real.mkdir(mode=0o700)
    alias = anchor / "alias"
    os.symlink(real, alias)

    with pytest.raises(SystemExit, match="not a trusted directory"):
        verifier._trusted_directory_chain(
            alias,
            anchor=anchor,
            owners={os.geteuid()},
            label="test pinned path",
        )


def test_release_verifier_rejects_peer_writable_ancestor(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "anchor"
    anchor.mkdir(mode=0o700)
    writable = anchor / "writable"
    writable.mkdir(mode=0o770)
    writable.chmod(0o770)

    with pytest.raises(SystemExit, match="not a trusted directory"):
        verifier._trusted_directory_chain(
            writable,
            anchor=anchor,
            owners={os.geteuid()},
            label="test pinned path",
        )


def test_release_verifier_file_hash_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"trusted")
    payload.chmod(0o600)
    alias = tmp_path / "alias"
    os.symlink(payload, alias)

    with pytest.raises(SystemExit, match="without following links"):
        verifier._sha256_file(
            alias,
            owners={os.geteuid()},
            label="test payload",
        )


def test_release_verifier_file_open_is_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"trusted")
    payload.chmod(0o600)
    opened_flags: list[int] = []

    def reject_open(path: Path, flags: int) -> int:
        assert path == payload
        opened_flags.append(flags)
        raise OSError("captured")

    monkeypatch.setattr(verifier.os, "open", reject_open)

    with pytest.raises(SystemExit, match="without following links"):
        verifier._read_trusted_regular_file(
            payload,
            owners={os.geteuid()},
            label="test payload",
        )

    assert len(opened_flags) == 1
    assert opened_flags[0] & os.O_NONBLOCK


def test_release_verifier_digest_snapshot_accepts_peer_writable_declaration(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "root"
    anchor.mkdir(mode=0o700)
    declarations = anchor / "ea"
    declarations.mkdir(mode=0o770)
    declarations.chmod(0o770)
    payload = declarations / "requirements.lock"
    payload.write_bytes(b"example==1.0\n")
    payload.chmod(0o660)
    expected_sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()

    captured = verifier._read_digest_authenticated_relative_file(
        anchor,
        "ea/requirements.lock",
        expected_sha256=expected_sha256,
        owners={os.geteuid()},
        label="test declaration",
    )

    assert captured == b"example==1.0\n"


def test_release_verifier_digest_snapshot_rejects_symlink_and_wrong_digest(
    tmp_path: Path,
) -> None:
    anchor = tmp_path / "root"
    anchor.mkdir(mode=0o700)
    declarations = anchor / "ea"
    declarations.mkdir(mode=0o770)
    payload = declarations / "real.lock"
    payload.write_bytes(b"example==1.0\n")
    alias = declarations / "requirements.lock"
    alias.symlink_to(payload.name)
    expected_sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()

    with pytest.raises(SystemExit, match="without following links"):
        verifier._read_digest_authenticated_relative_file(
            anchor,
            "ea/requirements.lock",
            expected_sha256=expected_sha256,
            owners={os.geteuid()},
            label="test declaration",
        )

    alias.unlink()
    alias.write_bytes(payload.read_bytes())
    alias.chmod(0o660)
    with pytest.raises(SystemExit, match="digest differs from the pin"):
        verifier._read_digest_authenticated_relative_file(
            anchor,
            "ea/requirements.lock",
            expected_sha256="0" * 64,
            owners={os.geteuid()},
            label="test declaration",
        )


def test_release_verifier_file_hash_rejects_stale_identity(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"trusted")
    payload.chmod(0o600)
    expected_metadata = payload.lstat()
    payload.chmod(0o400)

    with pytest.raises(SystemExit, match="changed before it was authenticated"):
        verifier._sha256_file(
            payload,
            owners={os.geteuid()},
            label="test payload",
            expected_metadata=expected_metadata,
        )
