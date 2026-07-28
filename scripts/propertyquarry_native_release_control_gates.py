#!/usr/bin/env python3
"""Run the complete pinned native release-control suite without accepted skips."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
NATIVE_ROOT = ROOT / "native" / "propertyquarry-release-control-v2"
LOCK_PATH = NATIVE_ROOT / "toolchain.lock.json"
NATIVE_TEST = ROOT / "tests" / "test_propertyquarry_release_control_native.py"
EXPECTED_NATIVE_TESTS = 55
NATIVE_TEMP_PREFIX = "pq-native."

EXPECTED_LOCK: dict[str, object] = {
    "arch": "amd64",
    "archive_bytes": 66_879_095,
    "archive_sha256": (
        "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
    ),
    "archive_url": "https://go.dev/dl/go1.26.5.linux-amd64.tar.gz",
    "distribution": "go.dev official binary archive",
    "go_binary_sha256": (
        "8da5fd321795754b994c64e3eb8a5a14ff47bd285559a7e876f3c79abafc67f9"
    ),
    "os": "linux",
    "schema": "propertyquarry.release-control.toolchain-lock.v2",
    "version": "go1.26.5",
}


class NativeGateError(RuntimeError):
    """The deterministic native release-control gate is not trustworthy."""


@dataclass(frozen=True)
class ValidatedInput:
    path: Path
    identity: tuple[int, int, int, int, int, int, int, int]


@dataclass(frozen=True)
class ValidatedToolchain:
    go_binary: ValidatedInput
    archive: ValidatedInput


def _reject_constant(raw: str) -> object:
    raise NativeGateError(f"non-finite JSON constant is forbidden: {raw}")


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NativeGateError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _load_lock() -> dict[str, object]:
    try:
        metadata = LOCK_PATH.lstat()
        raw = LOCK_PATH.read_bytes()
    except OSError as exc:
        raise NativeGateError("native toolchain lock is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise NativeGateError("native toolchain lock is not a trusted regular file")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeGateError("native toolchain lock is not strict JSON") from exc
    if payload != EXPECTED_LOCK:
        raise NativeGateError("native toolchain lock differs from the flagship pin")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise NativeGateError(f"cannot hash pinned input: {path}") from exc
    return digest.hexdigest()


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
    )


def _validate_input(
    raw_path: str,
    *,
    field: str,
    expected_size: int | None,
    expected_sha256: str,
    executable: bool,
) -> ValidatedInput:
    if not raw_path.strip():
        raise NativeGateError(f"{field} is required")
    candidate = Path(raw_path).expanduser()
    try:
        before = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        opened = resolved.stat()
    except OSError as exc:
        raise NativeGateError(f"{field} is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
        or opened.st_nlink != 1
        or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (executable and not opened.st_mode & stat.S_IXUSR)
    ):
        raise NativeGateError(f"{field} is not a trusted immutable regular file")
    if expected_size is not None and opened.st_size != expected_size:
        raise NativeGateError(f"{field} size differs from the flagship pin")
    if _sha256(resolved) != expected_sha256:
        raise NativeGateError(f"{field} digest differs from the flagship pin")
    after = resolved.stat()
    if _identity(after) != _identity(opened):
        raise NativeGateError(f"{field} changed while it was authenticated")
    return ValidatedInput(path=resolved, identity=_identity(after))


def validate_toolchain_inputs(
    environ: Mapping[str, str],
) -> ValidatedToolchain:
    lock = _load_lock()
    go_binary = _validate_input(
        str(environ.get("PROPERTYQUARRY_NATIVE_GO") or ""),
        field="PROPERTYQUARRY_NATIVE_GO",
        expected_size=None,
        expected_sha256=str(lock["go_binary_sha256"]),
        executable=True,
    )
    archive = _validate_input(
        str(environ.get("PROPERTYQUARRY_GO_ARCHIVE") or ""),
        field="PROPERTYQUARRY_GO_ARCHIVE",
        expected_size=int(lock["archive_bytes"]),
        expected_sha256=str(lock["archive_sha256"]),
        executable=False,
    )
    version = subprocess.run(
        [str(go_binary.path), "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "GOTELEMETRY": "off",
            "GOTOOLCHAIN": "local",
        },
    )
    if (
        version.returncode != 0
        or version.stdout != "go version go1.26.5 linux/amd64\n"
        or version.stderr
    ):
        raise NativeGateError(
            "PROPERTYQUARRY_NATIVE_GO is not the pinned Go 1.26.5 linux/amd64 toolchain"
        )
    return ValidatedToolchain(go_binary=go_binary, archive=archive)


def junit_counts(path: Path) -> tuple[int, int, int, int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise NativeGateError("native pytest did not produce valid JUnit evidence") from exc
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall("./testsuite"))
    else:
        raise NativeGateError("native pytest JUnit evidence has an invalid root")
    if not suites:
        raise NativeGateError("native pytest JUnit evidence contains no test suite")
    try:
        tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
        skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
        failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
        errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    except ValueError as exc:
        raise NativeGateError("native pytest JUnit counts are invalid") from exc
    if min(tests, skipped, failures, errors) < 0:
        raise NativeGateError("native pytest JUnit counts are invalid")

    testcase_count = 0
    testcase_skipped = 0
    testcase_failures = 0
    testcase_errors = 0
    testcase_identities: set[tuple[str, str]] = set()
    for suite in suites:
        for testcase in suite.findall("./testcase"):
            testcase_count += 1
            identity = (
                str(testcase.attrib.get("classname") or ""),
                str(testcase.attrib.get("name") or ""),
            )
            if not all(identity) or identity in testcase_identities:
                raise NativeGateError(
                    "native pytest JUnit testcase identities are missing or duplicated"
                )
            testcase_identities.add(identity)
            dispositions = [
                child.tag
                for child in testcase
                if child.tag in {"skipped", "failure", "error"}
            ]
            if len(dispositions) > 1:
                raise NativeGateError(
                    "native pytest JUnit testcase has multiple outcomes"
                )
            if dispositions == ["skipped"]:
                testcase_skipped += 1
            elif dispositions == ["failure"]:
                testcase_failures += 1
            elif dispositions == ["error"]:
                testcase_errors += 1
    if (
        testcase_count,
        testcase_skipped,
        testcase_failures,
        testcase_errors,
    ) != (tests, skipped, failures, errors):
        raise NativeGateError(
            "native pytest JUnit testcase evidence does not match declared counts"
        )
    return tests, skipped, failures, errors


def validate_junit_result(path: Path) -> int:
    tests, skipped, failures, errors = junit_counts(path)
    if tests != EXPECTED_NATIVE_TESTS or skipped or failures or errors:
        raise NativeGateError(
            "native suite must execute the pinned complete collection without skips, "
            "failures, or errors; "
            f"expected_tests={EXPECTED_NATIVE_TESTS}, "
            f"tests={tests}, skipped={skipped}, failures={failures}, errors={errors}"
        )
    return tests


def _revalidate_input(
    value: ValidatedInput,
    *,
    field: str,
    expected_size: int | None,
    expected_sha256: str,
    executable: bool,
) -> None:
    current = _validate_input(
        str(value.path),
        field=field,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        executable=executable,
    )
    if current.identity != value.identity:
        raise NativeGateError(f"{field} changed during the native suite")


def _native_test_environment(
    toolchain: ValidatedToolchain,
    temporary_directory: Path,
) -> dict[str, str]:
    home = temporary_directory / "home"
    go_cache = temporary_directory / "go-cache"
    go_module_cache = temporary_directory / "go-module-cache"
    for directory in (home, go_cache, go_module_cache):
        directory.mkdir(mode=0o700)
    return {
        "CGO_ENABLED": "0",
        "GOCACHE": str(go_cache),
        "GOMODCACHE": str(go_module_cache),
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PROPERTYQUARRY_GO_ARCHIVE": str(toolchain.archive.path),
        "PROPERTYQUARRY_NATIVE_GO": str(toolchain.go_binary.path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join(
            (str(ROOT / "scripts"), str(ROOT / "ea"), str(ROOT))
        ),
        "PYTHONSAFEPATH": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TMPDIR": str(temporary_directory),
        "TZ": "UTC",
    }


def run_native_gate(environ: Mapping[str, str]) -> int:
    toolchain = validate_toolchain_inputs(environ)
    with tempfile.TemporaryDirectory(
        prefix=NATIVE_TEMP_PREFIX,
        dir="/tmp",
    ) as temporary_directory:
        temporary_path = Path(temporary_directory)
        junit_path = temporary_path / "native-suite.xml"
        pytest_base_temp = temporary_path / "pytest"
        child_environment = _native_test_environment(toolchain, temporary_path)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--strict-config",
                "--strict-markers",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                f"--basetemp={pytest_base_temp}",
                f"--junitxml={junit_path}",
                str(NATIVE_TEST),
            ],
            cwd=ROOT,
            env=child_environment,
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
        tests = validate_junit_result(junit_path)
    _revalidate_input(
        toolchain.go_binary,
        field="PROPERTYQUARRY_NATIVE_GO",
        expected_size=None,
        expected_sha256=str(EXPECTED_LOCK["go_binary_sha256"]),
        executable=True,
    )
    _revalidate_input(
        toolchain.archive,
        field="PROPERTYQUARRY_GO_ARCHIVE",
        expected_size=int(EXPECTED_LOCK["archive_bytes"]),
        expected_sha256=str(EXPECTED_LOCK["archive_sha256"]),
        executable=False,
    )
    print(f"native-release-control-gate-ok tests={tests}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run every native PropertyQuarry release-control-v2 test using the "
            "explicit checksum-pinned Go binary and archive."
        ),
        epilog=(
            "Usage: set PROPERTYQUARRY_NATIVE_GO and PROPERTYQUARRY_GO_ARCHIVE, "
            "then run make propertyquarry-native-release-control-gates."
        ),
    )
    parser.parse_args(argv)
    try:
        return run_native_gate(os.environ)
    except (NativeGateError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
