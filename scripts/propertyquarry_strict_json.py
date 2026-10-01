#!/usr/bin/env python3
"""Bounded, duplicate-safe JSON snapshots for PropertyQuarry release gates."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Sequence


DEFAULT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_NODES = 200_000


class StrictJsonError(ValueError):
    pass


def _reject_constant(value: str) -> object:
    raise StrictJsonError(f"non_finite_json_constant:{value}")


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _validate_shape(
    value: object,
    *,
    maximum_depth: int,
    maximum_nodes: int,
) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > maximum_nodes:
            raise StrictJsonError("json_node_limit_exceeded")
        if depth > maximum_depth:
            raise StrictJsonError("json_depth_limit_exceeded")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def loads_strict_json_object(
    raw: bytes,
    *,
    field: str = "JSON artifact",
    maximum_bytes: int = DEFAULT_MAX_BYTES,
    maximum_depth: int = DEFAULT_MAX_DEPTH,
    maximum_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, Any]:
    if not raw or len(raw) > maximum_bytes:
        raise StrictJsonError(f"{field}:invalid_size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJsonError(f"{field}:invalid_utf8") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except StrictJsonError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise StrictJsonError(f"{field}:invalid_json") from exc
    if not isinstance(payload, dict):
        raise StrictJsonError(f"{field}:json_root_not_object")
    _validate_shape(
        payload,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
    )
    return payload


def _stable_file_bytes(
    path: Path,
    *,
    field: str,
    maximum_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = path.lstat()
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StrictJsonError(f"{field}:unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != before_path.st_dev
            or before.st_ino != before_path.st_ino
            or before.st_size < 1
            or before.st_size > maximum_bytes
        ):
            raise StrictJsonError(f"{field}:invalid_file")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise StrictJsonError(f"{field}:changed_while_read") from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (
        len(raw) != before.st_size
        or len(raw) > maximum_bytes
        or any(getattr(before, name) != getattr(after, name) for name in stable_fields)
        or any(getattr(before, name) != getattr(after_path, name) for name in stable_fields)
    ):
        raise StrictJsonError(f"{field}:changed_while_read")
    return raw


def load_strict_json_object_snapshot(
    path: Path,
    *,
    field: str = "JSON artifact",
    maximum_bytes: int = DEFAULT_MAX_BYTES,
    maximum_depth: int = DEFAULT_MAX_DEPTH,
    maximum_nodes: int = DEFAULT_MAX_NODES,
) -> tuple[dict[str, Any], bytes, str]:
    raw = _stable_file_bytes(path, field=field, maximum_bytes=maximum_bytes)
    payload = loads_strict_json_object(
        raw,
        field=field,
        maximum_bytes=maximum_bytes,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
    )
    return payload, raw, hashlib.sha256(raw).hexdigest()
