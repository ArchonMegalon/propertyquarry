#!/usr/bin/env python3
"""Environment-safe intake and subprocess scoping for live probe credentials."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Iterator


MAX_RELEASE_PROBE_SECRET_BYTES = 4_096
RELEASE_PROBE_SECRET_ENV_NAMES = (
    "PROPERTYQUARRY_LIVE_PROBE_SECRET",
    "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET",
)
MAX_EDGE_ASSERTION_SECRET_BYTES = 4_096
EDGE_ASSERTION_SECRET_ENV_NAMES = (
    "EA_EDGE_PRINCIPAL_ASSERTION_SECRET",
    "PROPERTYQUARRY_LIVE_EDGE_PRINCIPAL_ASSERTION_SECRET",
)


def release_probe_secret_environment_configured() -> bool:
    return any(os.environ.get(name) for name in RELEASE_PROBE_SECRET_ENV_NAMES)


def edge_assertion_secret_environment_configured() -> bool:
    return any(os.environ.get(name) for name in EDGE_ASSERTION_SECRET_ENV_NAMES)


def _read_bounded_secret_from_stdin(
    parser: argparse.ArgumentParser,
    *,
    label: str,
    max_bytes: int,
) -> str:
    raw = sys.stdin.buffer.read(max_bytes + 2)
    if len(raw) > max_bytes + 1:
        parser.error(f"{label} credential stdin exceeds {max_bytes} bytes")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        parser.error(f"{label} credential stdin must be UTF-8")
    secret = decoded[:-1] if decoded.endswith("\n") else decoded
    if secret.endswith("\r"):
        secret = secret[:-1]
    if (
        not secret
        or "\x00" in secret
        or "\r" in secret
        or "\n" in secret
        or len(secret.encode("utf-8")) > max_bytes
    ):
        parser.error(f"{label} credential stdin is malformed")
    return secret


def read_release_probe_secret_from_stdin(
    parser: argparse.ArgumentParser,
    *,
    enabled: bool,
) -> str:
    """Read one UTF-8 credential without accepting it in argv or the environment."""

    if release_probe_secret_environment_configured():
        parser.error(
            "release-probe credentials must not be supplied in the process environment; "
            "use --release-probe-secret-stdin"
        )
    if not enabled:
        return ""
    # A shell here-string contributes one trailing newline. Read enough to
    # accept the bounded credential plus that delimiter, but reject any larger
    # stream before the credential is used.
    return _read_bounded_secret_from_stdin(
        parser,
        label="release-probe",
        max_bytes=MAX_RELEASE_PROBE_SECRET_BYTES,
    )


def read_edge_assertion_secret_from_stdin(
    parser: argparse.ArgumentParser,
    *,
    enabled: bool,
) -> str:
    """Read one edge assertion secret without accepting it in argv or env."""

    if edge_assertion_secret_environment_configured():
        parser.error(
            "edge-assertion credentials must not be supplied in the process environment; "
            "use --edge-assertion-secret-stdin"
        )
    if not enabled:
        return ""
    return _read_bounded_secret_from_stdin(
        parser,
        label="edge-assertion",
        max_bytes=MAX_EDGE_ASSERTION_SECRET_BYTES,
    )


@contextlib.contextmanager
def release_probe_secret_environment_scrubbed() -> Iterator[None]:
    """Temporarily prevent probe credentials from reaching a child process."""

    retained = {
        name: os.environ.pop(name)
        for name in RELEASE_PROBE_SECRET_ENV_NAMES
        if name in os.environ
    }
    try:
        yield
    finally:
        for name in RELEASE_PROBE_SECRET_ENV_NAMES:
            os.environ.pop(name, None)
        os.environ.update(retained)


def scrub_release_probe_secret_environment() -> None:
    """Permanently remove probe credentials before any subprocess is started."""

    for name in RELEASE_PROBE_SECRET_ENV_NAMES:
        os.environ.pop(name, None)


def scrub_edge_assertion_secret_environment() -> None:
    """Permanently remove edge assertion credentials before child processes."""

    for name in EDGE_ASSERTION_SECRET_ENV_NAMES:
        os.environ.pop(name, None)
