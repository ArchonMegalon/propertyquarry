#!/usr/bin/env python3
"""Closed dispatcher for authenticated repository release Make targets.

GNU Make parses caller-controlled startup files and command-line makefiles
before a Makefile can defend itself.  Release automation must therefore invoke
this dispatcher through ``propertyquarry_release_python.sh`` instead of
trusting a caller-provided ``make`` command line.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Iterator, Mapping, NamedTuple, Sequence


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = Path("/docker/property")
MAKE = Path("/usr/bin/make")
MAKEFILE = CANONICAL_ROOT / "Makefile"
RELEASE_PYTHON = (
    CANONICAL_ROOT
    / ".propertyquarry_release_tools"
    / "release-venv"
    / "bin"
    / "python"
)
PLAYWRIGHT_BROWSERS = (
    CANONICAL_ROOT / ".propertyquarry_release_tools" / "ms-playwright"
)
PRIVATE_RUNTIME_PARENT = Path("/tmp")
# Keep the authenticated runtime root short enough for nested AF_UNIX sockets.
# Linux counts the entire pathname against sockaddr_un.sun_path (107 payload
# bytes), while pytest adds several deterministic directory components.
PRIVATE_RUNTIME_PREFIX = "pqrd."
PRIVATE_RUNTIME_DIRECTORIES = {
    "XDG_CACHE_HOME": "cache",
    "XDG_CONFIG_HOME": "config",
    "XDG_DATA_HOME": "data",
    "XDG_STATE_HOME": "state",
}
TRUSTED_SESSION_RUNTIME_DIRECTORY = (
    Path("/run") / "user" / str(os.geteuid())
)

DISPATCH_TARGETS = frozenset(
    {
        "ci-gates-authenticated",
        "ltd-release-gates",
        "materialize-release-assets-authenticated",
        "property-release-gates",
        "property-security-posture",
        "propertyquarry-native-release-control-gates",
        "propertyquarry-release-preflight",
        "propertyquarry-release-protocol-contracts",
        "release-preflight",
        "verify-design-full-mirror-parity",
        "verify-design-mirror-bundle",
        "verify-flagship-release-readiness-authenticated",
        "verify-generated-release-artifacts-clean-authenticated",
        "verify-ltd-critical-entries-authenticated",
        "verify-ltd-flagship-subset-authenticated",
        "verify-pocket-audio-archive",
        "verify-release-assets-authenticated",
    }
)

FORBIDDEN_ENVIRONMENT = frozenset(
    {
        "GNUMAKEFLAGS",
        "MAKECMDGOALS",
        "MAKEFILES",
        "MAKEFLAGS",
        "MAKELEVEL",
        "MAKEOVERRIDES",
        "MAKE_RESTARTS",
        "MFLAGS",
        "PROPERTYQUARRY_RELEASE_DISPATCH",
        "PYTEST_ADDOPTS",
        "PYTEST_DEBUG_TEMPROOT",
        "PYTEST_PLUGINS",
        "PYTEST_PYTHON_BIN",
        "PYTHON_BIN",
        "TEST_API_PYTEST_DESELECT",
        "TEST_API_PYTEST_IGNORE",
    }
)

PASSTHROUGH_ENVIRONMENT = frozenset(
    {
        "GITHUB_ACTIONS",
        "GITHUB_BASE_REF",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_HEAD_REF",
        "GITHUB_JOB",
        "GITHUB_REF",
        "GITHUB_REF_NAME",
        "GITHUB_REPOSITORY",
        "GITHUB_REPOSITORY_OWNER",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_NUMBER",
        "GITHUB_SERVER_URL",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_WORKSPACE",
        "PROPERTYQUARRY_GO_ARCHIVE",
        "PROPERTYQUARRY_NATIVE_GO",
        "RUNNER_ARCH",
        "RUNNER_OS",
        "RUNNER_TEMP",
        "RUNNER_TOOL_CACHE",
    }
)

PROPERTY_RELEASE_GATE_REQUIRED_ENVIRONMENT = frozenset(
    {
        "PROPERTYQUARRY_ACTIVATION_TO_VALUE_RECEIPT",
        "PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT",
        "PROPERTYQUARRY_CONTINUOUS_UX_RECEIPT",
        "PROPERTYQUARRY_DASHBOARD_RENDER_RECEIPT",
        "PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT",
        "PROPERTYQUARRY_DR_BACKUP_RECEIPT",
        "PROPERTYQUARRY_DR_RESTORE_RECEIPT",
        "PROPERTYQUARRY_EVIDENCE_OVERLAY_RECEIPT",
        "PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256",
        "PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN",
        "PROPERTYQUARRY_FAILURE_STATE_RECEIPT",
        "PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT",
        "PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT",
        "PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE",
        "PROPERTYQUARRY_PROVIDER_CATALOG_RECEIPT",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST",
        "PROPERTYQUARRY_RYBBIT_EVIDENCE_RECEIPT",
        "PROPERTYQUARRY_RYBBIT_ORIGIN",
        "PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256",
        "PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT",
        "PROPERTYQUARRY_SLO_METRICS_SNAPSHOT",
        "PROPERTYQUARRY_STRUCTURED_LOG_QUERY_RECEIPT",
    }
)

PROPERTY_RELEASE_GATE_OPTIONAL_ENVIRONMENT = frozenset(
    {
        "COMPOSE_PROJECT_NAME",
        "EA_PRINCIPAL_ID",
        "EA_PUBLIC_TOUR_DIR",
        "PROPERTYQUARRY_3D_BROWSER_GATE_BASE_URL",
        "PROPERTYQUARRY_API_CONTAINER_NAME",
        "PROPERTYQUARRY_COMPOSE_FILE",
        "PROPERTYQUARRY_COMPOSE_PROJECT_NAME",
        "PROPERTYQUARRY_DR_RELEASE_MAX_AGE_SECONDS",
        "PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_BASE_URL",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_PRINCIPAL_ID",
        "PROPERTYQUARRY_GOLD_NOTIFICATION_STATE",
        "PROPERTYQUARRY_LIVE_HOST_HEADER",
        "PROPERTYQUARRY_LIVE_MOBILE_BASE_URL",
        "PROPERTYQUARRY_LIVE_PRINCIPAL_ID",
        "PROPERTYQUARRY_LIVE_SMOKE_BASE_URL",
        "PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME",
        "PROPERTYQUARRY_PUBLIC_ORIGIN",
        "PROPERTYQUARRY_RENDER_CONTAINER_NAME",
        "PROPERTYQUARRY_RENDER_SERVICE",
        "PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_BASE_URL",
        "PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_CONTAINER",
        "PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_SMOKE_SLUG",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_BASE_URL",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_PRINCIPAL_ID",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_STATE",
        "PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE",
        "PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_RUNTIME_FILE",
        "PROPERTYQUARRY_SENT_LINKS_MANIFEST",
        "PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_BASE_URL",
        "PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_SMOKE_SLUG",
        "PROPERTYQUARRY_SLO_EVIDENCE_RECEIPT",
        "PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR",
        "PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR",
        "PROPERTYQUARRY_VISUAL_WATCH_INTERVAL_SECONDS",
        "PROPERTYQUARRY_VISUAL_WATCH_MOBILE_VIEWPORT",
        "PROPERTYQUARRY_VISUAL_WATCH_OUTPUT_DIR",
        "PROPERTYQUARRY_VISUAL_WATCH_SAMPLES",
        "PROPERTYQUARRY_VISUAL_WATCH_URL",
        "PROPERTYQUARRY_VISUAL_WATCH_VIEWPORT",
        "PROPERTYQUARRY_WALKTHROUGH_PROVIDER_PROOF_TIMEOUT_SECONDS",
        "PROPERTYQUARRY_WALKTHROUGH_PROVIDER_PROOF_TOUR_ROOT",
        "PROPERTYQUARRY_WALKTHROUGH_QUALITY_FFPROBE_TIMEOUT_SECONDS",
        "PROPERTYQUARRY_WALKTHROUGH_QUALITY_FRAME_SAMPLE_TIMEOUT_SECONDS",
        "PROPERTYQUARRY_WALKTHROUGH_QUALITY_PROCESS_TIMEOUT_SECONDS",
    }
)

PROPERTY_RELEASE_GATE_LIVE_REQUIRED_ENVIRONMENT = frozenset(
    {
        "DATABASE_URL",
        "EA_API_TOKEN",
        "PROPERTYQUARRY_EVIDENCE_OVERLAY_TEABLE_BASE_ID",
        "PROPERTYQUARRY_EXPECTED_RELEASE_ARTIFACT_SET",
        "PROPERTYQUARRY_EXPECTED_RELEASE_BRANCH",
        "PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID",
        "PROPERTYQUARRY_EXPECTED_RELEASE_GENERATED_AT",
        "PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST",
        "PROPERTYQUARRY_EXPECTED_RELEASE_LABEL",
        "PROPERTYQUARRY_EXPECTED_RELEASE_REPOSITORY",
        "PROPERTYQUARRY_EXPECTED_RENDER_IMAGE",
        "PROPERTYQUARRY_EXPECTED_REPLICA_ID",
        "PROPERTYQUARRY_EXPECTED_WEB_IMAGE",
        "PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE",
        "PROPERTYQUARRY_LIVE_TELEGRAM_BOT_TOKEN",
        "PROPERTYQUARRY_LIVE_TELEGRAM_CHAT_ID",
        "PROPERTYQUARRY_RELEASE_SECURITY_RECEIPT",
        "PROPERTYQUARRY_RELEASE_SECURITY_WORKFLOW_BINDING",
        "PROPERTYQUARRY_RYBBIT_API_KEY",
        "PROPERTYQUARRY_RYBBIT_EVENTS_API_URL",
        "PROPERTYQUARRY_RYBBIT_HAS_DATA_API_URL",
        "PROPERTYQUARRY_RYBBIT_SITE_API_URL",
        "PROPERTYQUARRY_RYBBIT_SITE_ID",
        "PROPERTYQUARRY_WORKFLOW_HEAD_SHA",
        "PROPERTYQUARRY_WORKFLOW_RUN_ATTEMPT",
        "PROPERTYQUARRY_WORKFLOW_RUN_ID",
        "TEABLE_API_KEY",
        "TEABLE_BASE_URL",
    }
)

PROPERTY_RELEASE_GATE_LIVE_OPTIONAL_ENVIRONMENT = frozenset(
    {
        "PROPERTYQUARRY_AXE_CORE_PATH",
        "PROPERTYQUARRY_LIVE_MOBILE_REQUIRED_BROWSER_ENGINES",
        "PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL",
    }
)

PROPERTY_RELEASE_GATE_ENVIRONMENT = (
    PROPERTY_RELEASE_GATE_REQUIRED_ENVIRONMENT
    | PROPERTY_RELEASE_GATE_OPTIONAL_ENVIRONMENT
    | PROPERTY_RELEASE_GATE_LIVE_REQUIRED_ENVIRONMENT
    | PROPERTY_RELEASE_GATE_LIVE_OPTIONAL_ENVIRONMENT
)


class DispatchError(RuntimeError):
    """The requested repository release dispatch is not trustworthy."""


class _DirectoryIdentity(NamedTuple):
    device: int
    inode: int
    owner: int
    mode: int


def make_command(target: str) -> tuple[str, ...]:
    if target not in DISPATCH_TARGETS:
        raise DispatchError(f"unsupported authenticated release target: {target}")
    return (
        str(MAKE),
        "--no-print-directory",
        "-rR",
        f"--file={MAKEFILE}",
        target,
    )


def child_environment(
    environ: Mapping[str, str],
    *,
    target: str | None = None,
) -> dict[str, str]:
    if target is not None and target not in DISPATCH_TARGETS:
        raise DispatchError(
            f"unsupported authenticated release target: {target}"
        )
    forbidden = sorted(name for name in FORBIDDEN_ENVIRONMENT if name in environ)
    if forbidden:
        raise DispatchError(
            "caller-controlled release build environment is forbidden: "
            + ", ".join(forbidden)
        )
    result = {
        "CI": "1",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PLAYWRIGHT_BROWSERS_PATH": str(PLAYWRIGHT_BROWSERS),
        "PROPERTYQUARRY_RELEASE_DISPATCH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "TMPDIR": "/tmp",
        "TZ": "UTC",
    }
    passthrough = PASSTHROUGH_ENVIRONMENT
    if target == "property-release-gates":
        passthrough = passthrough | PROPERTY_RELEASE_GATE_ENVIRONMENT
    for name in passthrough:
        if name in environ:
            result[name] = environ[name]
    return result


def _private_directory_identity(path: Path, *, label: str) -> _DirectoryIdentity:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DispatchError(f"{label} is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or mode != 0o700
    ):
        raise DispatchError(f"{label} is not an owner-only directory")
    return _DirectoryIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        mode,
    )


def _validate_private_runtime_parent() -> None:
    try:
        metadata = PRIVATE_RUNTIME_PARENT.lstat()
    except OSError as exc:
        raise DispatchError("private runtime parent is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or not metadata.st_mode & stat.S_ISVTX
    ):
        raise DispatchError("private runtime parent is not a trusted directory")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise DispatchError(
            "private runtime cleanup requires symlink-safe directory removal"
        )


def _validate_private_runtime_path(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.parent != PRIVATE_RUNTIME_PARENT
        or not path.name.startswith(PRIVATE_RUNTIME_PREFIX)
        or path.name == PRIVATE_RUNTIME_PREFIX
    ):
        raise DispatchError("private runtime path is outside the trusted namespace")


def _cleanup_private_runtime_root(
    path: Path,
    expected_identity: _DirectoryIdentity,
) -> None:
    _validate_private_runtime_path(path)
    observed_identity = _private_directory_identity(
        path,
        label="private runtime root",
    )
    if observed_identity != expected_identity:
        raise DispatchError("private runtime root identity changed before cleanup")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise DispatchError("private runtime root cleanup failed") from exc
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DispatchError(
            "private runtime root cleanup could not be verified"
        ) from exc
    raise DispatchError("private runtime root remains after cleanup")


def _trusted_session_runtime_directory() -> Path | None:
    """Return the derived owner-only user runtime, when this host has one."""

    path = TRUSTED_SESSION_RUNTIME_DIRECTORY
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DispatchError("trusted session runtime is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DispatchError("trusted session runtime cannot be resolved") from exc
    if (
        not path.is_absolute()
        or resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or mode != 0o700
    ):
        raise DispatchError(
            "trusted session runtime is not an owner-only directory"
        )
    return path


@contextmanager
def private_runtime_environment() -> Iterator[dict[str, str]]:
    """Create closed XDG state while retaining a validated user-session bus."""

    _validate_private_runtime_parent()
    path = Path(
        tempfile.mkdtemp(
            prefix=PRIVATE_RUNTIME_PREFIX,
            dir=PRIVATE_RUNTIME_PARENT,
        )
    )
    path.chmod(0o700)
    identity = _private_directory_identity(path, label="private runtime root")
    try:
        environment: dict[str, str] = {}
        for variable, directory_name in PRIVATE_RUNTIME_DIRECTORIES.items():
            directory = path / directory_name
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
            _private_directory_identity(
                directory,
                label=f"{variable} directory",
            )
            environment[variable] = str(directory)
        pytest_temp_root = path / "pytest"
        pytest_temp_root.mkdir(mode=0o700)
        pytest_temp_root.chmod(0o700)
        _private_directory_identity(
            pytest_temp_root,
            label="PYTEST_DEBUG_TEMPROOT directory",
        )
        environment["PYTEST_DEBUG_TEMPROOT"] = str(pytest_temp_root)
        session_runtime = _trusted_session_runtime_directory()
        if session_runtime is None:
            session_runtime = path / "runtime"
            session_runtime.mkdir(mode=0o700)
            session_runtime.chmod(0o700)
            _private_directory_identity(
                session_runtime,
                label="XDG_RUNTIME_DIR directory",
            )
        environment["XDG_RUNTIME_DIR"] = str(session_runtime)
        yield environment
    finally:
        _cleanup_private_runtime_root(path, identity)


def _trusted_regular_file(path: Path, *, owners: set[int], label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DispatchError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid not in owners
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise DispatchError(f"{label} is not a trusted regular file")


def validate_runtime() -> None:
    if ROOT != CANONICAL_ROOT or Path.cwd().resolve() != CANONICAL_ROOT:
        raise DispatchError("authenticated release dispatch requires /docker/property")
    try:
        root_metadata = CANONICAL_ROOT.lstat()
    except OSError as exc:
        raise DispatchError("canonical repository root is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid not in {0, os.geteuid()}
        or root_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise DispatchError("canonical repository root is not a trusted directory")
    if Path(os.path.abspath(sys.executable)) != RELEASE_PYTHON:
        raise DispatchError(
            "authenticated release dispatch requires the hash-locked "
            "release interpreter"
        )
    _trusted_regular_file(
        RELEASE_PYTHON,
        owners={0, os.geteuid()},
        label="release interpreter",
    )
    _trusted_regular_file(MAKE, owners={0}, label="system Make")
    _trusted_regular_file(
        MAKEFILE,
        owners={0, os.geteuid()},
        label="canonical Makefile",
    )


def dispatch(target: str, environ: Mapping[str, str]) -> int:
    validate_runtime()
    command = make_command(target)
    with private_runtime_environment() as runtime_environment:
        environment = child_environment(environ, target=target)
        environment.update(runtime_environment)
        completed = subprocess.run(
            command,
            cwd=CANONICAL_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            check=False,
        )
    return completed.returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        usage="propertyquarry_release_make_dispatch.py {authenticated-target}",
        description=(
            "Run one authenticated repository release target through a fixed "
            "GNU Make invocation. Invoke this script via "
            "scripts/propertyquarry_release_python.sh."
        ),
        epilog=(
            "Usage: invoke only through "
            "scripts/propertyquarry_release_python.sh."
        ),
    )
    parser.add_argument("target", choices=sorted(DISPATCH_TARGETS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return dispatch(args.target, os.environ)
    except (DispatchError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
