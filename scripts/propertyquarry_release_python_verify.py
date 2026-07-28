#!/usr/bin/python3.12
"""Authenticate the Python runtime used by flagship release gates.

The pin commits the interpreter, root-owned standard library, and private
environment. Root-owned libc, dynamic-loader, and transitive shared-library
state, plus changes made by the same uid after verification, remain explicit
trusted-host boundaries.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import BinaryIO, NoReturn


ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = ROOT / "config" / "propertyquarry_release_python_pin.json"
SOURCE_PYTHON = Path("/usr/bin/python3.12")
SOURCE_STDLIB = Path("/usr/lib/python3.12")
SOURCE_SITECUSTOMIZE = Path("/etc/python3.12/sitecustomize.py")
SOURCE_LIBPYTHON_LINK = Path(
    "/usr/lib/x86_64-linux-gnu/libpython3.12.so.1"
)
SOURCE_LIBPYTHON_LINK_TARGET = "libpython3.12.so.1.0"
SOURCE_LIBPYTHON = Path(
    "/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0"
)
VENV_RELATIVE = ".propertyquarry_release_tools/release-venv"
REQUIREMENTS_INPUT_RELATIVE = (
    "config/propertyquarry_release_verifier_requirements.in"
)
REQUIREMENTS_BASE_LOCK_RELATIVE = "ea/requirements.lock"
REQUIREMENTS_LOCK_RELATIVE = (
    "config/propertyquarry_release_verifier_requirements.lock"
)
VENV_ALLOWED_SYMLINKS = {"lib64": "lib"}
STDLIB_ALLOWED_SYMLINKS = {
    "_sysconfigdata__linux_x86_64-linux-gnu.py": (
        "_sysconfigdata__x86_64-linux-gnu.py"
    ),
    "config-3.12-x86_64-linux-gnu/libpython3.12.so": (
        "../../x86_64-linux-gnu/libpython3.12.so.1"
    ),
    "sitecustomize.py": "/etc/python3.12/sitecustomize.py",
}
EXPECTED_PIN_KEYS = {
    "schema",
    "python_binary",
    "python_binary_sha256",
    "python_libpython",
    "python_libpython_link",
    "python_libpython_link_target",
    "python_libpython_mode",
    "python_libpython_sha256",
    "python_sitecustomize",
    "python_sitecustomize_mode",
    "python_sitecustomize_sha256",
    "python_stdlib",
    "python_stdlib_entry_count",
    "python_stdlib_regular_bytes",
    "python_stdlib_tree_sha256",
    "python_version",
    "requirements_input",
    "requirements_input_sha256",
    "requirements_base_lock",
    "requirements_base_lock_sha256",
    "requirements_lock",
    "requirements_lock_sha256",
    "venv",
    "venv_entry_count",
    "venv_regular_bytes",
    "venv_tree_sha256",
}
_REQUIREMENT_NAME_PATTERN = (
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
)
_REQUIREMENT_VERSION_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9.!+_-]*[A-Za-z0-9])?"
_REQUIREMENT_EXTRA_PATTERN = (
    rf"{_REQUIREMENT_NAME_PATTERN}"
    rf"(?:\s*,\s*{_REQUIREMENT_NAME_PATTERN})*"
)
_INPUT_PIN_PATTERN = re.compile(
    rf"^(?P<name>{_REQUIREMENT_NAME_PATTERN})"
    rf"(?P<extras>\[(?:{_REQUIREMENT_EXTRA_PATTERN})\])?"
    rf"==(?P<version>{_REQUIREMENT_VERSION_PATTERN})"
    r"(?:\s+#.*)?$"
)
_BASE_LOCK_PIN_PATTERN = re.compile(
    rf"^(?P<name>{_REQUIREMENT_NAME_PATTERN})"
    rf"==(?P<version>{_REQUIREMENT_VERSION_PATTERN})"
    r"(?:\s+#.*)?$"
)
_INCLUDE_PATTERN = re.compile(
    r"^-r\s+\.\./ea/requirements\.lock(?:\s+#.*)?$"
)
_LOCK_PIN_PATTERN = re.compile(
    rf"^(?P<name>{_REQUIREMENT_NAME_PATTERN})"
    rf"==(?P<version>{_REQUIREMENT_VERSION_PATTERN})"
    r"(?P<continued>\s+\\)?$"
)
_LOCK_HASH_PATTERN = re.compile(
    r"^--hash=sha256:[0-9a-f]{64}(?P<continued>\s+\\)?$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_DIGEST_AUTHENTICATED_REQUIREMENTS_BYTES = 1024 * 1024


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"error: release Python verification failed: {message}")


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_regular_metadata(
    metadata: os.stat_result,
    *,
    owners: set[int],
    label: str,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in owners
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        _fail(f"{label} is not a trusted regular file")


def _trusted_regular_file(
    path: Path,
    *,
    owners: set[int],
    label: str,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail(f"{label} is unavailable: {exc}")
    _validate_regular_metadata(metadata, owners=owners, label=label)
    return metadata


@contextmanager
def _trusted_regular_file_handle(
    path: Path,
    *,
    owners: set[int],
    label: str,
    expected_metadata: os.stat_result | None = None,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(f"{label} cannot be opened without following links: {exc}")
    try:
        opened_metadata = os.fstat(descriptor)
        _validate_regular_metadata(
            opened_metadata,
            owners=owners,
            label=label,
        )
        try:
            path_metadata = path.lstat()
        except OSError as exc:
            _fail(f"{label} changed while it was opened: {exc}")
        if _metadata_identity(path_metadata) != _metadata_identity(opened_metadata):
            _fail(f"{label} changed while it was opened")
        if (
            expected_metadata is not None
            and _metadata_identity(expected_metadata)
            != _metadata_identity(opened_metadata)
        ):
            _fail(f"{label} changed before it was authenticated")

        try:
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                yield handle, opened_metadata
        except BaseException:
            raise
        else:
            after_metadata = os.fstat(descriptor)
            try:
                path_after = path.lstat()
            except OSError as exc:
                _fail(f"{label} changed during authentication: {exc}")
            if (
                _metadata_identity(after_metadata)
                != _metadata_identity(opened_metadata)
                or _metadata_identity(path_after)
                != _metadata_identity(opened_metadata)
            ):
                _fail(f"{label} changed during authentication")
    finally:
        os.close(descriptor)


def _sha256_file(
    path: Path,
    *,
    owners: set[int],
    label: str,
    expected_metadata: os.stat_result | None = None,
) -> str:
    digest = hashlib.sha256()
    with _trusted_regular_file_handle(
        path,
        owners=owners,
        label=label,
        expected_metadata=expected_metadata,
    ) as (handle, _):
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_trusted_regular_file(
    path: Path,
    *,
    owners: set[int],
    label: str,
) -> bytes:
    with _trusted_regular_file_handle(
        path,
        owners=owners,
        label=label,
    ) as (handle, _):
        return handle.read()


def _read_digest_authenticated_relative_file(
    anchor: Path,
    relative: str,
    *,
    expected_sha256: str,
    owners: set[int],
    label: str,
) -> bytes:
    """Read a hash-pinned declaration below a trusted anchor without links.

    Descendant directories and the final file may be peer-writable. Their
    names are resolved relative to pinned directory descriptors, the opened
    object identities must stay stable, and only the expected digest is
    accepted. This is suitable for declaration inputs that are not reopened
    for execution after verification.
    """

    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or relative_path.as_posix() != relative
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        _fail(f"{label} path is not a canonical relative path")
    if _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        _fail(f"{label} digest pin is invalid")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_handles: list[
        tuple[int, int | None, str | None, tuple[int, ...]]
    ] = []
    file_descriptor: int | None = None
    try:
        try:
            anchor_descriptor = os.open(anchor, directory_flags)
        except OSError as exc:
            _fail(f"{label} anchor cannot be opened without following links: {exc}")
        anchor_metadata = os.fstat(anchor_descriptor)
        if (
            not stat.S_ISDIR(anchor_metadata.st_mode)
            or anchor_metadata.st_uid not in owners
            or anchor_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            os.close(anchor_descriptor)
            _fail(f"{label} anchor is not a trusted directory")
        try:
            anchor_path_metadata = anchor.lstat()
        except OSError as exc:
            os.close(anchor_descriptor)
            _fail(f"{label} anchor changed while it was opened: {exc}")
        anchor_identity = _metadata_identity(anchor_metadata)
        if _metadata_identity(anchor_path_metadata) != anchor_identity:
            os.close(anchor_descriptor)
            _fail(f"{label} anchor changed while it was opened")
        directory_handles.append(
            (anchor_descriptor, None, None, anchor_identity)
        )

        for component in relative_path.parts[:-1]:
            parent_descriptor = directory_handles[-1][0]
            try:
                descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                _fail(
                    f"{label} ancestor {component} cannot be opened without "
                    f"following links: {exc}"
                )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in owners
            ):
                os.close(descriptor)
                _fail(f"{label} ancestor {component} is not an owned directory")
            try:
                path_metadata = os.stat(
                    component,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                os.close(descriptor)
                _fail(f"{label} ancestor {component} changed while opened: {exc}")
            identity = _metadata_identity(metadata)
            if _metadata_identity(path_metadata) != identity:
                os.close(descriptor)
                _fail(f"{label} ancestor {component} changed while opened")
            directory_handles.append(
                (descriptor, parent_descriptor, component, identity)
            )

        filename = relative_path.parts[-1]
        parent_descriptor = directory_handles[-1][0]
        try:
            file_descriptor = os.open(
                filename,
                file_flags,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            _fail(f"{label} cannot be opened without following links: {exc}")
        opened_metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or opened_metadata.st_uid not in owners
        ):
            _fail(f"{label} is not an owned regular file")
        try:
            path_metadata = os.stat(
                filename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            _fail(f"{label} changed while it was opened: {exc}")
        opened_identity = _metadata_identity(opened_metadata)
        if _metadata_identity(path_metadata) != opened_identity:
            _fail(f"{label} changed while it was opened")

        payload = bytearray()
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > _MAX_DIGEST_AUTHENTICATED_REQUIREMENTS_BYTES:
                _fail(f"{label} exceeds the authenticated size limit")

        after_metadata = os.fstat(file_descriptor)
        try:
            path_after = os.stat(
                filename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            _fail(f"{label} changed during authentication: {exc}")
        if (
            _metadata_identity(after_metadata) != opened_identity
            or _metadata_identity(path_after) != opened_identity
        ):
            _fail(f"{label} changed during authentication")
        for (
            descriptor,
            ancestor_parent,
            component,
            identity,
        ) in reversed(directory_handles):
            if ancestor_parent is None:
                try:
                    path_after = anchor.lstat()
                except OSError as exc:
                    _fail(f"{label} anchor changed during authentication: {exc}")
            else:
                try:
                    path_after = os.stat(
                        str(component),
                        dir_fd=ancestor_parent,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    _fail(
                        f"{label} ancestor {component} changed during "
                        f"authentication: {exc}"
                    )
            if (
                _metadata_identity(os.fstat(descriptor)) != identity
                or _metadata_identity(path_after) != identity
            ):
                _fail(f"{label} path changed during authentication")

        result = bytes(payload)
        if hashlib.sha256(result).hexdigest() != expected_sha256:
            _fail(f"{label} digest differs from the pin")
        return result
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor, _, _, _ in reversed(directory_handles):
            os.close(descriptor)


def _trusted_directory(
    path: Path,
    *,
    owners: set[int],
    label: str,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail(f"{label} is unavailable: {exc}")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in owners
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        _fail(f"{label} is not a trusted directory")
    return metadata


def _trusted_symlink(
    path: Path,
    *,
    owners: set[int],
    expected_target: str,
    label: str,
) -> os.stat_result:
    try:
        metadata = path.lstat()
        target = os.readlink(path)
        metadata_after = path.lstat()
    except OSError as exc:
        _fail(f"{label} is unavailable: {exc}")
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid not in owners
        or metadata.st_nlink != 1
        or target != expected_target
        or _metadata_identity(metadata_after) != _metadata_identity(metadata)
    ):
        _fail(f"{label} is not the pinned trusted symbolic link")
    return metadata


def _trusted_directory_chain(
    path: Path,
    *,
    anchor: Path,
    owners: set[int],
    label: str,
) -> None:
    if not path.is_absolute() or not anchor.is_absolute():
        _fail(f"{label} path is not absolute")
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        _fail(f"{label} path escapes its trusted anchor")
    current = anchor
    _trusted_directory(current, owners=owners, label=f"{label} ancestor {current}")
    for component in relative.parts:
        current = current / component
        _trusted_directory(
            current,
            owners=owners,
            label=f"{label} ancestor {current}",
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate pin key: {key}")
        result[key] = value
    return result


def _normalized_requirement_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _decode_requirements(
    payload: bytes,
    *,
    label: str,
) -> tuple[str | None, list[str]]:
    try:
        return payload.decode("utf-8"), []
    except UnicodeDecodeError as exc:
        return None, [f"{label} is not valid UTF-8: {exc}"]


def _input_direct_pins(
    payload: bytes,
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    text, issues = _decode_requirements(
        payload,
        label="release verifier requirements input",
    )
    if text is None:
        return {}, issues
    pins: dict[str, tuple[str, str]] = {}
    include_count = 0
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if _INCLUDE_PATTERN.fullmatch(line):
            include_count += 1
            if include_count > 1:
                issues.append(
                    "release verifier requirements input repeats the "
                    "canonical -r ../ea/requirements.lock include"
                )
            continue
        match = _INPUT_PIN_PATTERN.fullmatch(line)
        if match is None:
            issues.append(
                "release verifier requirements input line "
                f"{line_number} is not an exact direct pin or requirement include"
            )
            continue
        normalized = _normalized_requirement_name(match.group("name"))
        version = match.group("version")
        display = (
            f"{match.group('name')}{match.group('extras') or ''}=={version}"
        )
        existing = pins.get(normalized)
        if existing is not None:
            qualifier = (
                "conflicting" if existing[0] != version else "duplicate"
            )
            issues.append(
                "release verifier requirements input has "
                f"{qualifier} direct pins for {normalized}: "
                f"{existing[1]} and {display}"
            )
            continue
        pins[normalized] = (version, display)
    if not pins:
        issues.append("release verifier requirements input has no direct pins")
    if include_count == 0:
        issues.append(
            "release verifier requirements input is missing the canonical "
            "-r ../ea/requirements.lock include"
        )
    return pins, issues


def _compiled_lock_pins(
    payload: bytes,
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    text, issues = _decode_requirements(
        payload,
        label="release verifier requirements lock",
    )
    if text is None:
        return {}, issues
    pins: dict[str, tuple[str, str]] = {}
    current_display: str | None = None
    current_has_hash = False
    current_continues = False

    def finish_current_pin() -> None:
        if current_display is not None and not current_has_hash:
            issues.append(
                f"{current_display} has no sha256 hash in the compiled "
                "requirements lock"
            )
        elif current_display is not None and current_continues:
            issues.append(
                f"{current_display} has an unterminated hash continuation in "
                "the compiled requirements lock"
            )

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line:
            continue
        if raw_line[0].isspace():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            hash_match = _LOCK_HASH_PATTERN.fullmatch(line)
            if (
                current_display is None
                or not current_continues
                or hash_match is None
            ):
                issues.append(
                    "release verifier requirements lock line "
                    f"{line_number} is not a compiled sha256 hash tail"
                )
            else:
                current_has_hash = True
                current_continues = (
                    hash_match.group("continued") is not None
                )
            continue
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        finish_current_pin()
        match = _LOCK_PIN_PATTERN.fullmatch(line)
        if match is None:
            current_display = None
            current_has_hash = False
            current_continues = False
            issues.append(
                "release verifier requirements lock line "
                f"{line_number} is not an exact compiled pin"
            )
            continue
        name = match.group("name")
        normalized = _normalized_requirement_name(name)
        version = match.group("version")
        display = f"{name}=={version}"
        existing = pins.get(normalized)
        if existing is not None:
            qualifier = (
                "conflicting" if existing[0] != version else "duplicate"
            )
            issues.append(
                "release verifier requirements lock has "
                f"{qualifier} pins for {normalized}: "
                f"{existing[1]} and {display}"
            )
        else:
            pins[normalized] = (version, display)
        current_display = display
        current_has_hash = False
        current_continues = match.group("continued") is not None
    finish_current_pin()
    if not pins:
        issues.append("release verifier requirements lock has no compiled pins")
    return pins, issues


def _base_lock_pins(
    payload: bytes,
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    text, issues = _decode_requirements(
        payload,
        label="release verifier base requirements lock",
    )
    if text is None:
        return {}, issues
    pins: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _BASE_LOCK_PIN_PATTERN.fullmatch(line)
        if match is None:
            issues.append(
                "release verifier base requirements lock line "
                f"{line_number} is not an exact pin"
            )
            continue
        name = match.group("name")
        normalized = _normalized_requirement_name(name)
        version = match.group("version")
        display = f"{name}=={version}"
        existing = pins.get(normalized)
        if existing is not None:
            qualifier = (
                "conflicting" if existing[0] != version else "duplicate"
            )
            issues.append(
                "release verifier base requirements lock has "
                f"{qualifier} pins for {normalized}: "
                f"{existing[1]} and {display}"
            )
            continue
        pins[normalized] = (version, display)
    if not pins:
        issues.append("release verifier base requirements lock has no pins")
    return pins, issues


def requirements_lock_issues(
    requirements_input: bytes,
    requirements_base_lock: bytes,
    requirements_lock: bytes,
) -> list[str]:
    """Return fail-closed input-closure parity issues for a compiled lock."""

    input_pins, input_issues = _input_direct_pins(requirements_input)
    base_pins, base_issues = _base_lock_pins(requirements_base_lock)
    lock_pins, lock_issues = _compiled_lock_pins(requirements_lock)
    issues = [*input_issues, *base_issues, *lock_issues]
    for normalized, (expected_version, display) in sorted(base_pins.items()):
        locked = lock_pins.get(normalized)
        if locked is None:
            issues.append(
                f"base requirement {display} is missing from the compiled "
                "requirements lock"
            )
        elif locked[0] != expected_version:
            issues.append(
                f"base requirement {display} resolves to {locked[1]} in the "
                "compiled requirements lock"
            )
    for normalized, (expected_version, display) in sorted(input_pins.items()):
        locked = lock_pins.get(normalized)
        if locked is None:
            issues.append(
                f"{display} is missing from the compiled requirements lock"
            )
        elif locked[0] != expected_version:
            issues.append(
                f"{display} resolves to {locked[1]} in the compiled "
                "requirements lock"
            )
    return issues


def _load_pin() -> dict[str, object]:
    trusted_repo_owners = {0, os.geteuid()}
    _trusted_directory_chain(
        ROOT,
        anchor=Path("/"),
        owners=trusted_repo_owners,
        label="repository root",
    )
    _trusted_directory_chain(
        PIN_PATH.parent,
        anchor=ROOT,
        owners=trusted_repo_owners,
        label="release Python pin",
    )
    raw_pin = _read_trusted_regular_file(
        PIN_PATH,
        owners=trusted_repo_owners,
        label="release Python pin",
    )
    try:
        payload = json.loads(
            raw_pin.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _fail(
                f"non-finite pin value is forbidden: {value}"
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"release Python pin is invalid: {exc}")
    if not isinstance(payload, dict) or set(payload) != EXPECTED_PIN_KEYS:
        _fail("release Python pin keys differ from the v3 contract")
    if payload.get("schema") != "propertyquarry.release-python-pin.v3":
        _fail("release Python pin schema is invalid")
    return payload


def _resolve_pinned_repo_path(
    value: object,
    *,
    expected: str,
    label: str,
    directory: bool,
) -> Path:
    relative = str(value or "")
    if relative != expected:
        _fail(f"{label} path differs from the flagship pin")
    candidate = ROOT / relative
    if not candidate.is_relative_to(ROOT):
        _fail(f"{label} escapes the repository")
    trusted_repo_owners = {0, os.geteuid()}
    _trusted_directory_chain(
        candidate if directory else candidate.parent,
        anchor=ROOT,
        owners=trusted_repo_owners,
        label=label,
    )
    if not directory:
        _trusted_regular_file(
            candidate,
            owners=trusted_repo_owners,
            label=label,
        )
    return candidate


def _resolve_pinned_system_directory(
    value: object,
    *,
    expected: Path,
    label: str,
) -> Path:
    candidate = Path(str(value or ""))
    if candidate != expected:
        _fail(f"{label} path differs from the flagship pin")
    _trusted_directory_chain(
        candidate,
        anchor=Path("/"),
        owners={0},
        label=label,
    )
    return candidate


def _resolve_pinned_system_regular_file(
    value: object,
    *,
    expected: Path,
    label: str,
) -> tuple[Path, os.stat_result]:
    candidate = Path(str(value or ""))
    if candidate != expected:
        _fail(f"{label} path differs from the flagship pin")
    _trusted_directory_chain(
        candidate.parent,
        anchor=Path("/"),
        owners={0},
        label=label,
    )
    return candidate, _trusted_regular_file(
        candidate,
        owners={0},
        label=label,
    )


def _pinned_mode(value: object, *, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 0o7777:
        _fail(f"{label} mode is invalid")
    return value


def _verified_requirements_contract(pin: Mapping[str, object]) -> None:
    owners = {0, os.geteuid()}
    input_path = _resolve_pinned_repo_path(
        pin["requirements_input"],
        expected=REQUIREMENTS_INPUT_RELATIVE,
        label="release verifier requirements input",
        directory=False,
    )
    input_payload = _read_trusted_regular_file(
        input_path,
        owners=owners,
        label="release verifier requirements input",
    )
    if (
        hashlib.sha256(input_payload).hexdigest()
        != str(pin["requirements_input_sha256"])
    ):
        _fail("release verifier requirements input digest differs from the pin")

    base_lock_relative = str(pin["requirements_base_lock"] or "")
    if base_lock_relative != REQUIREMENTS_BASE_LOCK_RELATIVE:
        _fail(
            "release verifier base requirements lock path differs from the "
            "flagship pin"
        )
    base_lock_payload = _read_digest_authenticated_relative_file(
        ROOT,
        base_lock_relative,
        expected_sha256=str(pin["requirements_base_lock_sha256"] or ""),
        owners=owners,
        label="release verifier base requirements lock",
    )

    lock_path = _resolve_pinned_repo_path(
        pin["requirements_lock"],
        expected=REQUIREMENTS_LOCK_RELATIVE,
        label="release verifier requirements lock",
        directory=False,
    )
    lock_payload = _read_trusted_regular_file(
        lock_path,
        owners=owners,
        label="release verifier requirements lock",
    )
    if (
        hashlib.sha256(lock_payload).hexdigest()
        != str(pin["requirements_lock_sha256"])
    ):
        _fail("release verifier requirements lock digest differs from the pin")

    issues = requirements_lock_issues(
        input_payload,
        base_lock_payload,
        lock_payload,
    )
    if issues:
        _fail(
            "release verifier requirements input/lock is stale: "
            + "; ".join(issues)
        )


def _tree_paths(root: Path) -> list[Path]:
    return [
        root,
        *sorted(
            root.rglob("*"),
            key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
        ),
    ]


def _tree_relative_text(path: Path, root: Path) -> str:
    return "." if path == root else path.relative_to(root).as_posix()


def _tree_digest(
    root: Path,
    *,
    owners: set[int] | None = None,
    allowed_symlinks: Mapping[str, str] | None = None,
    forbid_bytecode: bool = True,
    label: str = "verifier environment",
) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    entry_count = 0
    regular_bytes = 0
    trusted_owners = {0, os.geteuid()} if owners is None else owners
    trusted_symlinks = (
        VENV_ALLOWED_SYMLINKS if allowed_symlinks is None else allowed_symlinks
    )
    paths = _tree_paths(root)
    initial_relative_paths = [
        _tree_relative_text(path, root) for path in paths
    ]
    identities: dict[str, tuple[int, ...]] = {}
    for path, relative_text in zip(paths, initial_relative_paths, strict=True):
        relative = relative_text.encode("utf-8")
        try:
            metadata = path.lstat()
        except OSError as exc:
            _fail(f"{label} entry is unavailable: {exc}")
        identities[relative_text] = _metadata_identity(metadata)
        if metadata.st_uid not in trusted_owners:
            _fail(f"{label} entry has an untrusted owner: {relative_text}")
        if (
            not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            _fail(f"{label} entry is peer-writable: {relative_text}")
        relative_path = Path(relative_text)
        if forbid_bytecode and (
            "__pycache__" in relative_path.parts or path.suffix == ".pyc"
        ):
            _fail(f"mutable Python bytecode is forbidden: {relative_text}")

        if stat.S_ISDIR(metadata.st_mode):
            kind = b"D"
            payload = b""
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                _fail(f"{label} hard link is forbidden: {relative_text}")
            kind = b"F"
            content_digest = bytes.fromhex(
                _sha256_file(
                    path,
                    owners=trusted_owners,
                    label=f"{label} entry {relative_text}",
                    expected_metadata=metadata,
                )
            )
            payload = metadata.st_size.to_bytes(8, "big") + content_digest
            regular_bytes += metadata.st_size
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            if trusted_symlinks.get(relative_text) != target:
                _fail(f"{label} symlink is forbidden: {relative_text}")
            kind = b"L"
            target_bytes = os.fsencode(target)
            payload = len(target_bytes).to_bytes(8, "big") + target_bytes
        else:
            _fail(f"{label} special file is forbidden: {relative_text}")

        owner_kind = b"R" if metadata.st_uid == 0 else b"E"
        permissions = (stat.S_IMODE(metadata.st_mode) & 0o7777).to_bytes(2, "big")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(kind)
        digest.update(owner_kind)
        digest.update(permissions)
        digest.update(payload)
        entry_count += 1

    after_paths = _tree_paths(root)
    after_relative_paths = [
        _tree_relative_text(path, root) for path in after_paths
    ]
    if after_relative_paths != initial_relative_paths:
        _fail(f"{label} changed during authentication")
    for path, relative_text in zip(
        after_paths,
        after_relative_paths,
        strict=True,
    ):
        try:
            metadata = path.lstat()
        except OSError as exc:
            _fail(f"{label} changed during authentication: {exc}")
        if _metadata_identity(metadata) != identities[relative_text]:
            _fail(f"{label} changed during authentication: {relative_text}")
    return digest.hexdigest(), entry_count, regular_bytes


def verified_interpreter() -> Path:
    pin = _load_pin()
    source_python = Path(str(pin["python_binary"]))
    if source_python != SOURCE_PYTHON:
        _fail("source interpreter path differs from the flagship pin")
    _trusted_directory_chain(
        source_python.parent,
        anchor=Path("/"),
        owners={0},
        label="source interpreter",
    )
    source_metadata = _trusted_regular_file(
        source_python,
        owners={0},
        label="source interpreter",
    )
    if Path(sys.executable).resolve() != source_python:
        _fail("verifier is not running under the pinned source interpreter")
    if (
        _sha256_file(
            source_python,
            owners={0},
            label="source interpreter",
            expected_metadata=source_metadata,
        )
        != str(pin["python_binary_sha256"])
    ):
        _fail("source interpreter digest differs from the flagship pin")
    expected_version = str(pin["python_version"])
    actual_version = ".".join(str(value) for value in sys.version_info[:3])
    if actual_version != expected_version:
        _fail("source interpreter version differs from the flagship pin")

    _verified_requirements_contract(pin)

    stdlib = _resolve_pinned_system_directory(
        pin["python_stdlib"],
        expected=SOURCE_STDLIB,
        label="source standard library",
    )
    (
        stdlib_tree_sha256,
        stdlib_entry_count,
        stdlib_regular_bytes,
    ) = _tree_digest(
        stdlib,
        owners={0},
        allowed_symlinks=STDLIB_ALLOWED_SYMLINKS,
        forbid_bytecode=False,
        label="source standard library",
    )
    if stdlib_tree_sha256 != str(pin["python_stdlib_tree_sha256"]):
        _fail("source standard library tree digest differs from the pin")
    if stdlib_entry_count != pin["python_stdlib_entry_count"]:
        _fail("source standard library entry count differs from the pin")
    if stdlib_regular_bytes != pin["python_stdlib_regular_bytes"]:
        _fail("source standard library byte count differs from the pin")

    sitecustomize, sitecustomize_metadata = (
        _resolve_pinned_system_regular_file(
            pin["python_sitecustomize"],
            expected=SOURCE_SITECUSTOMIZE,
            label="source sitecustomize",
        )
    )
    if (
        stat.S_IMODE(sitecustomize_metadata.st_mode)
        != _pinned_mode(
            pin["python_sitecustomize_mode"],
            label="source sitecustomize",
        )
    ):
        _fail("source sitecustomize mode differs from the pin")
    if (
        _sha256_file(
            sitecustomize,
            owners={0},
            label="source sitecustomize",
            expected_metadata=sitecustomize_metadata,
        )
        != str(pin["python_sitecustomize_sha256"])
    ):
        _fail("source sitecustomize digest differs from the pin")

    libpython_link = Path(str(pin["python_libpython_link"] or ""))
    if libpython_link != SOURCE_LIBPYTHON_LINK:
        _fail("source libpython link path differs from the flagship pin")
    libpython_link_target = str(pin["python_libpython_link_target"] or "")
    if libpython_link_target != SOURCE_LIBPYTHON_LINK_TARGET:
        _fail("source libpython link target differs from the flagship pin")
    _trusted_directory_chain(
        libpython_link.parent,
        anchor=Path("/"),
        owners={0},
        label="source libpython link",
    )
    _trusted_symlink(
        libpython_link,
        owners={0},
        expected_target=libpython_link_target,
        label="source libpython link",
    )
    libpython, libpython_metadata = _resolve_pinned_system_regular_file(
        pin["python_libpython"],
        expected=SOURCE_LIBPYTHON,
        label="source libpython",
    )
    if (
        stat.S_IMODE(libpython_metadata.st_mode)
        != _pinned_mode(
            pin["python_libpython_mode"],
            label="source libpython",
        )
    ):
        _fail("source libpython mode differs from the pin")
    if (
        _sha256_file(
            libpython,
            owners={0},
            label="source libpython",
            expected_metadata=libpython_metadata,
        )
        != str(pin["python_libpython_sha256"])
    ):
        _fail("source libpython digest differs from the pin")

    venv = _resolve_pinned_repo_path(
        pin["venv"],
        expected=VENV_RELATIVE,
        label="release verifier environment",
        directory=True,
    )
    tree_sha256, entry_count, regular_bytes = _tree_digest(
        venv,
        owners={0, os.geteuid()},
        allowed_symlinks=VENV_ALLOWED_SYMLINKS,
        forbid_bytecode=True,
        label="release verifier environment",
    )
    if tree_sha256 != str(pin["venv_tree_sha256"]):
        _fail("release verifier environment tree digest differs from the pin")
    if entry_count != pin["venv_entry_count"]:
        _fail("release verifier environment entry count differs from the pin")
    if regular_bytes != pin["venv_regular_bytes"]:
        _fail("release verifier environment byte count differs from the pin")

    interpreter = venv / "bin" / "python"
    interpreter_metadata = _trusted_regular_file(
        interpreter,
        owners={0, os.geteuid()},
        label="release verifier interpreter",
    )
    if (
        _sha256_file(
            interpreter,
            owners={0, os.geteuid()},
            label="release verifier interpreter",
            expected_metadata=interpreter_metadata,
        )
        != str(pin["python_binary_sha256"])
    ):
        _fail("release verifier interpreter digest differs from the pin")
    return interpreter


def main() -> int:
    if sys.argv[1:] == ["--check-requirements-parity"]:
        _verified_requirements_contract(_load_pin())
        return 0
    if sys.argv[1:]:
        _fail("unsupported verifier arguments")
    print(verified_interpreter())
    return 0


def _entrypoint() -> int:
    try:
        return main()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
