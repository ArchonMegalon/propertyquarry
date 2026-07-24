#!/usr/bin/env python3
"""Deterministic, dependency-free PropertyQuarry screenshot baseline verification.

The verify path is deliberately read-only with respect to manifests and baselines.
Baseline replacement is available only through the explicit ``update`` command and
is refused whenever a recognised CI environment is active.
"""

from __future__ import annotations

import argparse
import binascii
import errno
import hashlib
import json
import math
import os
import re
import stat
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


MANIFEST_SCHEMA = "propertyquarry.visual_baseline_manifest.v1"
RECEIPT_SCHEMA = "propertyquarry.visual_baseline_receipt.v1"
COMPARISON_ALGORITHM = "yiq-perceptual-rgba-on-white.v1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
CASE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,119})")
MAX_MANIFEST_BYTES = 1_000_000
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_DIMENSION = 8_192
MAX_PIXELS = 25_000_000
MAX_CASES = 100
MAX_PNG_CHUNKS = 4_096
MAX_IDAT_BYTES = 48 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 1_000
MAX_UPDATE_TOTAL_BYTES = 512 * 1024 * 1024
YIQ_MAX_DELTA = 35_215.0
PIXEL_THRESHOLD = 0.1
MAX_CHANGED_PIXEL_RATIO = 0.005
SOURCE_BINDING_SCHEMA = "propertyquarry.release_hygiene_receipt.v1"
RELEASE_METADATA_DESCENDANT_PATHS = (
    ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
    ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
    ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
    "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
)
SOURCE_BINDING_REQUIRED_CHECKS = (
    "release_manifest_runtime_commit_matches_head_parent_or_metadata_only_ancestor",
    "tracked_worktree_clean",
    "no_untracked_release_source_files",
    "no_tracked_live_env_files",
    "no_tracked_audit_scratch_paths",
    "no_tracked_audit_artifacts",
    "no_hardcoded_local_api_token_marker",
    "no_raw_local_bridge_host_refs",
    "no_hardcoded_bearer_authorization",
)
SOURCE_BINDING_KEYS = {
    "schema",
    "generated_at",
    "status",
    "required_checks",
    "failure_count",
    "failures",
    "manifest_runtime_commit",
    "head_commit",
    "parent_commit",
    "merge_commit_required",
    "merge_base_parent_commit",
    "merge_parent_commits",
    "head_tree",
    "reviewed_envelope_commit",
    "reviewed_envelope_parent_commits",
    "reviewed_envelope_tree",
    "merge_tree_matches_reviewed_envelope",
    "manifest_descendant_paths",
    "manifest_metadata_only_ancestor",
    "tracked_dirty_path_count",
    "untracked_release_source_count",
    "note",
}
CI_ENV_NAMES = (
    "CI",
    "GITHUB_ACTIONS",
    "BUILDKITE",
    "CIRCLECI",
    "GITLAB_CI",
    "TF_BUILD",
)
CAPTURE_KEYS = {
    "browser_engine",
    "locale",
    "timezone_id",
    "device_scale_factor",
    "reduced_motion",
    "color_scheme",
    "service_workers",
    "animations",
    "caret",
}
CAPTURE_CONTRACT = {
    "browser_engine": "chromium",
    "locale": "en-US",
    "timezone_id": "UTC",
    "device_scale_factor": 1,
    "reduced_motion": "reduce",
    "color_scheme": "light",
    "service_workers": "block",
    "animations": "disabled",
    "caret": "hidden",
}
COMPARISON_KEYS = {
    "algorithm",
    "pixel_threshold",
    "max_changed_pixel_ratio",
}
MANIFEST_KEYS = {"schema", "version", "capture", "comparison", "cases"}
CASE_KEYS = {"id", "baseline", "width", "height", "sha256"}


class VisualBaselineError(ValueError):
    """A bounded, user-safe visual baseline contract failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DecodedPNG:
    width: int
    height: int
    rgba: bytes


@dataclass(frozen=True)
class PixelComparison:
    changed_pixel_count: int
    total_pixel_count: int
    changed_pixel_ratio: float
    maximum_yiq_delta: float
    diff_png: bytes


@dataclass(frozen=True)
class CasePathPlan:
    baseline_path: Path
    actual_path: Path
    diff_path: Path


@dataclass(frozen=True)
class PathNode:
    label: str
    role: str
    case_id: str
    path: Path


@dataclass
class VerificationPreflight:
    case_paths: dict[str, CasePathPlan]
    case_errors: dict[str, list[str]]
    errors: list[str]
    nodes: list[PathNode]
    expected_actual_names: list[str]
    observed_actual_names: list[str]
    extra_actual_names: list[str]
    missing_actual_names: list[str]
    path_graph_safe: bool
    actual_workspace_safe: bool
    diff_workspace_safe: bool
    receipt_write_allowed: bool
    protected_output_collision: bool


def _unreadable_code(missing_code: str) -> str:
    stem = missing_code.removesuffix("_missing")
    return f"{stem}_unreadable"


def _bounded_bytes(path: Path, *, limit: int, missing_code: str, large_code: str) -> bytes:
    """Read one regular file through a single non-following descriptor.

    The size check and reads share the same descriptor, closing the prior
    stat/is-file/read TOCTOU window.  ``O_NOFOLLOW`` is intentionally required on
    platforms that expose it; screenshot evidence must not be supplied through a
    last-component symlink.
    """

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise VisualBaselineError(missing_code) from exc
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise VisualBaselineError(missing_code) from exc
        raise VisualBaselineError(_unreadable_code(missing_code)) from exc
    try:
        try:
            stat_result = os.fstat(descriptor)
        except OSError as exc:
            raise VisualBaselineError(_unreadable_code(missing_code)) from exc
        if not stat.S_ISREG(stat_result.st_mode):
            raise VisualBaselineError(_unreadable_code(missing_code))
        if stat_result.st_size < 1 or stat_result.st_size > limit:
            raise VisualBaselineError(large_code)
        payload = bytearray()
        while True:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(payload)))
            except OSError as exc:
                raise VisualBaselineError(_unreadable_code(missing_code)) from exc
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > limit:
                raise VisualBaselineError(large_code)
        if len(payload) != stat_result.st_size:
            raise VisualBaselineError(large_code)
        return bytes(payload)
    finally:
        os.close(descriptor)


def _paeth(left: int, up: int, upper_left: int) -> int:
    prediction = left + up - upper_left
    left_distance = abs(prediction - left)
    up_distance = abs(prediction - up)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def decode_png(payload: bytes) -> DecodedPNG:
    """Decode bounded, non-interlaced 8-bit RGB/RGBA PNG data.

    Playwright emits this subset for ordinary page screenshots. CRCs, chunk bounds,
    zlib completion, row sizes, and every PNG filter are checked before pixels are
    exposed to the comparator.
    """

    if not isinstance(payload, bytes) or not payload.startswith(PNG_SIGNATURE):
        raise VisualBaselineError("png_signature_invalid")
    if len(payload) > MAX_PNG_BYTES:
        raise VisualBaselineError("png_input_too_large")
    offset = len(PNG_SIGNATURE)
    ihdr: bytes | None = None
    idat_parts: list[bytes] = []
    idat_bytes = 0
    chunk_count = 0
    saw_iend = False
    while offset < len(payload):
        chunk_count += 1
        if chunk_count > MAX_PNG_CHUNKS:
            raise VisualBaselineError("png_chunk_count_exceeded")
        if len(payload) - offset < 12:
            raise VisualBaselineError("png_chunk_truncated")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        offset += 4
        if length > MAX_PNG_BYTES or length > len(payload) - offset - 8:
            raise VisualBaselineError("png_chunk_length_invalid")
        chunk_type = payload[offset : offset + 4]
        offset += 4
        chunk_payload = payload[offset : offset + length]
        offset += length
        expected_crc = struct.unpack(">I", payload[offset : offset + 4])[0]
        offset += 4
        observed_crc = binascii.crc32(chunk_type + chunk_payload) & 0xFFFFFFFF
        if observed_crc != expected_crc:
            raise VisualBaselineError("png_chunk_crc_invalid")
        if chunk_type == b"IHDR":
            if ihdr is not None or idat_parts or length != 13:
                raise VisualBaselineError("png_ihdr_invalid")
            ihdr = chunk_payload
        elif chunk_type == b"IDAT":
            if ihdr is None or saw_iend:
                raise VisualBaselineError("png_idat_order_invalid")
            idat_bytes += len(chunk_payload)
            if idat_bytes > MAX_IDAT_BYTES:
                raise VisualBaselineError("png_idat_size_exceeded")
            idat_parts.append(chunk_payload)
        elif chunk_type == b"IEND":
            if length != 0 or saw_iend:
                raise VisualBaselineError("png_iend_invalid")
            saw_iend = True
            if offset != len(payload):
                raise VisualBaselineError("png_trailing_data_forbidden")
            break
        elif chunk_type and 65 <= chunk_type[0] <= 90:
            raise VisualBaselineError("png_unknown_critical_chunk")
    if ihdr is None or not idat_parts or not saw_iend:
        raise VisualBaselineError("png_required_chunks_missing")
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if (
        width < 1
        or height < 1
        or width > MAX_DIMENSION
        or height > MAX_DIMENSION
        or width * height > MAX_PIXELS
    ):
        raise VisualBaselineError("png_dimensions_out_of_bounds")
    if bit_depth != 8 or color_type not in {2, 6}:
        raise VisualBaselineError("png_color_format_unsupported")
    if compression != 0 or filter_method != 0 or interlace != 0:
        raise VisualBaselineError("png_encoding_unsupported")
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    expected_raw_size = height * (stride + 1)
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(b"".join(idat_parts), expected_raw_size + 1)
        if len(raw) <= expected_raw_size:
            raw += decoder.flush(expected_raw_size + 1 - len(raw))
    except zlib.error as exc:
        raise VisualBaselineError("png_zlib_invalid") from exc
    if (
        len(raw) != expected_raw_size
        or not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
    ):
        raise VisualBaselineError("png_decompressed_size_invalid")

    decoded = bytearray(height * stride)
    previous = bytearray(stride)
    raw_offset = 0
    output_offset = 0
    for _row_index in range(height):
        filter_type = raw[raw_offset]
        raw_offset += 1
        filtered = raw[raw_offset : raw_offset + stride]
        raw_offset += stride
        if filter_type > 4:
            raise VisualBaselineError("png_filter_invalid")
        current = bytearray(stride)
        for index, value in enumerate(filtered):
            left = current[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                reconstructed = value
            elif filter_type == 1:
                reconstructed = value + left
            elif filter_type == 2:
                reconstructed = value + up
            elif filter_type == 3:
                reconstructed = value + ((left + up) // 2)
            else:
                reconstructed = value + _paeth(left, up, upper_left)
            current[index] = reconstructed & 0xFF
        decoded[output_offset : output_offset + stride] = current
        output_offset += stride
        previous = current

    if color_type == 6:
        rgba = bytes(decoded)
    else:
        rgba_buffer = bytearray(width * height * 4)
        target = 0
        for source in range(0, len(decoded), 3):
            rgba_buffer[target : target + 4] = decoded[source : source + 3] + b"\xff"
            target += 4
        rgba = bytes(rgba_buffer)
    return DecodedPNG(width=width, height=height, rgba=rgba)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", binascii.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def encode_rgba_png(width: int, height: int, rgba: bytes) -> bytes:
    if (
        width < 1
        or height < 1
        or width > MAX_DIMENSION
        or height > MAX_DIMENSION
        or width * height > MAX_PIXELS
        or len(rgba) != width * height * 4
    ):
        raise VisualBaselineError("diff_dimensions_invalid")
    stride = width * 4
    rows = bytearray()
    for row_index in range(height):
        rows.append(0)
        start = row_index * stride
        rows.extend(rgba[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )


def _opaque_channel(channel: int, alpha: int) -> int:
    return (channel * alpha + 255 * (255 - alpha) + 127) // 255


def _opaque_rgb(rgba: bytes, offset: int) -> tuple[int, int, int]:
    alpha = rgba[offset + 3]
    return (
        _opaque_channel(rgba[offset], alpha),
        _opaque_channel(rgba[offset + 1], alpha),
        _opaque_channel(rgba[offset + 2], alpha),
    )


def _yiq_delta(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    red = first[0] - second[0]
    green = first[1] - second[1]
    blue = first[2] - second[2]
    luminance = 0.29889531 * red + 0.58662247 * green + 0.11448223 * blue
    in_phase = 0.59597799 * red - 0.27417610 * green - 0.32180189 * blue
    quadrature = 0.21147017 * red - 0.52261711 * green + 0.31114694 * blue
    return max(
        0.0,
        0.5053 * luminance * luminance
        + 0.299 * in_phase * in_phase
        + 0.1957 * quadrature * quadrature,
    )


def compare_pixels(
    baseline: DecodedPNG,
    actual: DecodedPNG,
    *,
    pixel_threshold: float,
) -> PixelComparison:
    if baseline.width != actual.width or baseline.height != actual.height:
        raise VisualBaselineError("png_dimension_mismatch")
    if not math.isfinite(pixel_threshold) or not 0.0 <= pixel_threshold <= 1.0:
        raise VisualBaselineError("pixel_threshold_invalid")
    changed = 0
    maximum_delta = 0.0
    diff = bytearray(len(baseline.rgba))
    for offset in range(0, len(baseline.rgba), 4):
        baseline_rgb = _opaque_rgb(baseline.rgba, offset)
        actual_rgb = _opaque_rgb(actual.rgba, offset)
        delta = _yiq_delta(baseline_rgb, actual_rgb)
        normalized_delta = math.sqrt(delta / YIQ_MAX_DELTA)
        maximum_delta = max(maximum_delta, normalized_delta)
        if normalized_delta > pixel_threshold:
            changed += 1
            strength = min(255, 96 + int(round(159 * normalized_delta)))
            diff[offset : offset + 4] = bytes((strength, 24, 48, 255))
        else:
            gray = int(
                round(
                    0.29889531 * baseline_rgb[0]
                    + 0.58662247 * baseline_rgb[1]
                    + 0.11448223 * baseline_rgb[2]
                )
            )
            quiet = 232 + (gray * 23 // 255)
            diff[offset : offset + 4] = bytes((quiet, quiet, quiet, 255))
    total = baseline.width * baseline.height
    return PixelComparison(
        changed_pixel_count=changed,
        total_pixel_count=total,
        changed_pixel_ratio=changed / total,
        maximum_yiq_delta=maximum_delta,
        diff_png=encode_rgba_png(baseline.width, baseline.height, bytes(diff)),
    )


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str], code: str) -> None:
    if set(payload) != expected:
        raise VisualBaselineError(code)


def _safe_relative_png(value: object, *, code: str) -> str:
    if type(value) is not str:
        raise VisualBaselineError(code)
    raw = value
    pure = PurePosixPath(raw)
    if (
        not raw
        or pure.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.suffix.lower() != ".png"
    ):
        raise VisualBaselineError(code)
    return pure.as_posix()


def validate_manifest(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise VisualBaselineError("manifest_object_required")
    _require_exact_keys(payload, MANIFEST_KEYS, "manifest_keys_invalid")
    if (
        type(payload.get("schema")) is not str
        or payload.get("schema") != MANIFEST_SCHEMA
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
    ):
        raise VisualBaselineError("manifest_schema_invalid")
    capture = payload.get("capture")
    if not isinstance(capture, dict):
        raise VisualBaselineError("manifest_capture_invalid")
    _require_exact_keys(capture, CAPTURE_KEYS, "manifest_capture_keys_invalid")
    if capture != CAPTURE_CONTRACT or any(
        type(capture[key]) is not type(expected)
        for key, expected in CAPTURE_CONTRACT.items()
    ):
        raise VisualBaselineError("manifest_capture_contract_invalid")
    comparison = payload.get("comparison")
    if not isinstance(comparison, dict):
        raise VisualBaselineError("manifest_comparison_invalid")
    _require_exact_keys(comparison, COMPARISON_KEYS, "manifest_comparison_keys_invalid")
    if (
        type(comparison.get("algorithm")) is not str
        or comparison.get("algorithm") != COMPARISON_ALGORITHM
    ):
        raise VisualBaselineError("manifest_algorithm_invalid")
    raw_pixel_threshold = comparison["pixel_threshold"]
    raw_max_ratio = comparison["max_changed_pixel_ratio"]
    if type(raw_pixel_threshold) not in {int, float} or type(raw_max_ratio) not in {
        int,
        float,
    }:
        raise VisualBaselineError("manifest_threshold_invalid")
    pixel_threshold = float(raw_pixel_threshold)
    max_ratio = float(raw_max_ratio)
    if (
        not math.isfinite(pixel_threshold)
        or pixel_threshold != PIXEL_THRESHOLD
        or not math.isfinite(max_ratio)
        or max_ratio != MAX_CHANGED_PIXEL_RATIO
    ):
        raise VisualBaselineError("manifest_threshold_invalid")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= MAX_CASES:
        raise VisualBaselineError("manifest_cases_invalid")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise VisualBaselineError("manifest_case_invalid")
        _require_exact_keys(raw_case, CASE_KEYS, "manifest_case_keys_invalid")
        raw_case_id = raw_case.get("id")
        if type(raw_case_id) is not str:
            raise VisualBaselineError("manifest_case_id_invalid")
        case_id = raw_case_id
        if CASE_ID_RE.fullmatch(case_id) is None or case_id in seen_ids:
            raise VisualBaselineError("manifest_case_id_invalid")
        baseline = _safe_relative_png(
            raw_case.get("baseline"), code="manifest_baseline_path_invalid"
        )
        if baseline in seen_paths:
            raise VisualBaselineError("manifest_baseline_path_duplicate")
        width = raw_case["width"]
        height = raw_case["height"]
        if (
            type(width) is not int
            or type(height) is not int
            or width < 1
            or height < 1
            or width > MAX_DIMENSION
            or height > MAX_DIMENSION
            or width * height > MAX_PIXELS
        ):
            raise VisualBaselineError("manifest_case_dimensions_invalid")
        raw_sha256 = raw_case.get("sha256")
        if type(raw_sha256) is not str:
            raise VisualBaselineError("manifest_case_sha256_invalid")
        sha256 = raw_sha256
        if SHA256_RE.fullmatch(sha256) is None:
            raise VisualBaselineError("manifest_case_sha256_invalid")
        cases.append(
            {
                "id": case_id,
                "baseline": baseline,
                "width": width,
                "height": height,
                "sha256": sha256,
            }
        )
        seen_ids.add(case_id)
        seen_paths.add(baseline)
    return {
        "schema": MANIFEST_SCHEMA,
        "version": 1,
        "capture": dict(CAPTURE_CONTRACT),
        "comparison": {
            "algorithm": COMPARISON_ALGORITHM,
            "pixel_threshold": pixel_threshold,
            "max_changed_pixel_ratio": max_ratio,
        },
        "cases": cases,
    }


def load_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    payload = _bounded_bytes(
        path,
        limit=MAX_MANIFEST_BYTES,
        missing_code="manifest_missing",
        large_code="manifest_size_invalid",
    )

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        decoded_object: dict[str, Any] = {}
        for key, value in pairs:
            if key in decoded_object:
                raise VisualBaselineError("manifest_json_duplicate_key")
            decoded_object[key] = value
        return decoded_object

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualBaselineError("manifest_json_invalid") from exc
    return validate_manifest(decoded), payload


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_temporary(path: Path, payload: bytes, *, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary_path
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _replace_staged(temporary_path: Path, path: Path, *, mode: int) -> None:
    os.replace(temporary_path, path)
    os.chmod(path, mode)
    _fsync_directory(path.parent)


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    temporary_path = _stage_temporary(path, payload, mode=mode)
    try:
        _replace_staged(temporary_path, path, mode=mode)
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(path, serialized, mode=mode)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_binding_payload_sha256(payload: object) -> str:
    serialized = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256(serialized)


def load_source_binding_receipt(path: Path) -> tuple[dict[str, Any], str]:
    raw = _bounded_bytes(
        path,
        limit=MAX_MANIFEST_BYTES,
        missing_code="source_binding_receipt_missing",
        large_code="source_binding_receipt_size_invalid",
    )

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        decoded_object: dict[str, Any] = {}
        for key, value in pairs:
            if key in decoded_object:
                raise VisualBaselineError("source_binding_json_duplicate_key")
            decoded_object[key] = value
        return decoded_object

    def reject_nonfinite_constant(_value: str) -> None:
        raise VisualBaselineError("source_binding_json_nonfinite_number")

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualBaselineError("source_binding_json_invalid") from exc
    if not isinstance(decoded, dict):
        raise VisualBaselineError("source_binding_object_required")
    return decoded, source_binding_payload_sha256(decoded)


def validate_source_binding_receipt(
    receipt: object,
    *,
    release_commit_sha: str,
    workflow_head_sha: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return False, ["source_binding_object_required"]
    if set(receipt) != SOURCE_BINDING_KEYS:
        errors.append("source_binding_keys_invalid")
    release_sha = (
        release_commit_sha.strip().lower()
        if type(release_commit_sha) is str
        else ""
    )
    workflow_sha = (
        workflow_head_sha.strip().lower()
        if type(workflow_head_sha) is str
        else ""
    )
    manifest_sha = receipt.get("manifest_runtime_commit")
    reported_head_sha = receipt.get("head_commit")
    parent_sha = receipt.get("parent_commit")
    if receipt.get("schema") != SOURCE_BINDING_SCHEMA:
        errors.append("source_binding_schema_invalid")
    if receipt.get("status") != "pass":
        errors.append("source_binding_status_not_pass")
    if receipt.get("required_checks") != list(SOURCE_BINDING_REQUIRED_CHECKS):
        errors.append("source_binding_checks_invalid")
    if (
        type(receipt.get("failure_count")) is not int
        or receipt.get("failure_count") != 0
        or receipt.get("failures") != []
    ):
        errors.append("source_binding_failures_present")
    if (
        COMMIT_SHA_RE.fullmatch(release_sha) is None
        or type(manifest_sha) is not str
        or manifest_sha != release_sha
    ):
        errors.append("source_binding_release_sha_mismatch")
    if (
        COMMIT_SHA_RE.fullmatch(workflow_sha) is None
        or type(reported_head_sha) is not str
        or reported_head_sha != workflow_sha
    ):
        errors.append("source_binding_workflow_sha_mismatch")
    if type(parent_sha) is not str or COMMIT_SHA_RE.fullmatch(parent_sha) is None:
        errors.append("source_binding_parent_sha_invalid")

    merge_parent_commits = receipt.get("merge_parent_commits")
    head_tree = receipt.get("head_tree")
    protected_topology_defaults = (
        receipt.get("merge_commit_required") is False
        and receipt.get("merge_base_parent_commit") == ""
        and receipt.get("reviewed_envelope_commit") == ""
        and receipt.get("reviewed_envelope_parent_commits") == []
        and receipt.get("reviewed_envelope_tree") == ""
        and receipt.get("merge_tree_matches_reviewed_envelope") is False
    )
    if not protected_topology_defaults:
        errors.append("source_binding_protected_topology_forbidden")
    if (
        type(merge_parent_commits) is not list
        or not 1 <= len(merge_parent_commits) <= 2
        or any(
            type(value) is not str or COMMIT_SHA_RE.fullmatch(value) is None
            for value in merge_parent_commits
        )
        or len(set(merge_parent_commits)) != len(merge_parent_commits)
        or (
            type(parent_sha) is str
            and COMMIT_SHA_RE.fullmatch(parent_sha) is not None
            and parent_sha not in merge_parent_commits
        )
        or workflow_sha in merge_parent_commits
        or type(head_tree) is not str
        or COMMIT_SHA_RE.fullmatch(head_tree) is None
    ):
        errors.append("source_binding_topology_invalid")

    descendant_paths = receipt.get("manifest_descendant_paths")
    metadata_only = receipt.get("manifest_metadata_only_ancestor")
    if workflow_sha == release_sha:
        if descendant_paths != [] or metadata_only is not False:
            errors.append("source_binding_same_commit_shape_invalid")
    elif (
        parent_sha == workflow_sha
        or descendant_paths != list(RELEASE_METADATA_DESCENDANT_PATHS)
        or metadata_only is not True
    ):
        errors.append("source_binding_metadata_envelope_invalid")
    if (
        type(receipt.get("tracked_dirty_path_count")) is not int
        or receipt.get("tracked_dirty_path_count") != 0
        or type(receipt.get("untracked_release_source_count")) is not int
        or receipt.get("untracked_release_source_count") != 0
    ):
        errors.append("source_binding_worktree_not_clean")
    generated_at = receipt.get("generated_at")
    try:
        generated_time = datetime.fromisoformat(
            generated_at.replace("Z", "+00:00")
            if type(generated_at) is str
            else ""
        )
    except ValueError:
        generated_time = None
    if generated_time is None or generated_time.tzinfo is None:
        errors.append("source_binding_generated_at_invalid")
    if type(receipt.get("note")) is not str or not receipt.get("note").strip():
        errors.append("source_binding_note_invalid")
    return not errors, list(dict.fromkeys(errors))


def _git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def _safe_join(root: Path, relative: str, *, code: str) -> Path:
    try:
        resolved_root = root.resolve()
        candidate = resolved_root / relative
        resolved = candidate.resolve()
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise VisualBaselineError(code) from exc
    return candidate


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _add_case_error(case_errors: dict[str, list[str]], case_id: str, code: str) -> None:
    _append_unique(case_errors.setdefault(case_id, []), code)


def _scan_png_directory(
    directory: Path,
    *,
    missing_ok: bool,
    missing_code: str,
    unreadable_code: str,
) -> tuple[list[str], dict[str, str], str]:
    names: list[str] = []
    entry_kinds: dict[str, str] = {}
    try:
        with os.scandir(directory) as entries:
            for entry_index, entry in enumerate(entries, start=1):
                if entry_index > MAX_DIRECTORY_ENTRIES:
                    return [], {}, f"{unreadable_code}_entry_limit_exceeded"
                if not entry.name.lower().endswith(".png"):
                    continue
                names.append(entry.name)
                try:
                    if entry.is_symlink():
                        entry_kinds[entry.name] = "symlink"
                    elif entry.is_file(follow_symlinks=False):
                        entry_kinds[entry.name] = "regular"
                    else:
                        entry_kinds[entry.name] = "non_regular"
                except OSError:
                    entry_kinds[entry.name] = "unreadable"
    except FileNotFoundError:
        return ([], {}, "") if missing_ok else ([], {}, missing_code)
    except OSError:
        return [], {}, unreadable_code
    return sorted(names), entry_kinds, ""


def _build_verification_preflight(
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    actual_dir: Path,
    diff_dir: Path,
    receipt_path: Path,
) -> VerificationPreflight:
    case_paths: dict[str, CasePathPlan] = {}
    case_errors: dict[str, list[str]] = {
        str(case["id"]): [] for case in manifest["cases"]
    }
    errors: list[str] = []
    nodes: list[PathNode] = [
        PathNode(label="manifest", role="manifest", case_id="", path=manifest_path),
        PathNode(label="receipt", role="receipt", case_id="", path=receipt_path),
    ]
    path_graph_safe = True
    actual_workspace_safe = True
    diff_workspace_safe = True
    receipt_write_allowed = True
    protected_output_collision = False

    expected_actual_names = [f"{case['id']}.png" for case in manifest["cases"]]
    expected_diff_names = [f"{case['id']}.diff.png" for case in manifest["cases"]]

    for case in manifest["cases"]:
        case_id = str(case["id"])
        resolved_paths: dict[str, Path] = {}
        path_specs = (
            (
                "baseline",
                manifest_path.parent,
                str(case["baseline"]),
                "baseline_path_escape",
            ),
            ("actual", actual_dir, f"{case_id}.png", "actual_path_escape"),
            ("diff", diff_dir, f"{case_id}.diff.png", "diff_path_escape"),
        )
        for role, root, relative, error_code in path_specs:
            try:
                resolved_paths[role] = _safe_join(root, relative, code=error_code)
            except VisualBaselineError as exc:
                _add_case_error(case_errors, case_id, exc.code)
                _append_unique(errors, f"{case_id}:{exc.code}")
                path_graph_safe = False
                if role == "diff":
                    diff_workspace_safe = False
        if set(resolved_paths) == {"baseline", "actual", "diff"}:
            case_paths[case_id] = CasePathPlan(
                baseline_path=resolved_paths["baseline"],
                actual_path=resolved_paths["actual"],
                diff_path=resolved_paths["diff"],
            )
        for role, path in resolved_paths.items():
            nodes.append(
                PathNode(
                    label=f"{role}:{case_id}",
                    role=role,
                    case_id=case_id,
                    path=path,
                )
            )

    observed_actual_names, actual_kinds, actual_scan_error = _scan_png_directory(
        actual_dir,
        missing_ok=False,
        missing_code="actual_dir_missing",
        unreadable_code="actual_dir_unreadable",
    )
    if actual_scan_error:
        _append_unique(errors, actual_scan_error)
        actual_workspace_safe = False
    expected_actual_set = set(expected_actual_names)
    observed_actual_set = set(observed_actual_names)
    extra_actual_names = sorted(observed_actual_set - expected_actual_set)
    missing_actual_names = sorted(expected_actual_set - observed_actual_set)
    if extra_actual_names or missing_actual_names:
        _append_unique(errors, "actual_png_set_mismatch")
        actual_workspace_safe = False
    for name, kind in actual_kinds.items():
        if kind == "regular":
            continue
        case_id = name.removesuffix(".png") if name in expected_actual_set else ""
        code = f"actual_{kind}_forbidden" if kind != "unreadable" else "actual_unreadable"
        _append_unique(errors, code)
        actual_workspace_safe = False
        path_graph_safe = False
        if case_id:
            _add_case_error(case_errors, case_id, code)

    observed_diff_names, diff_kinds, diff_scan_error = _scan_png_directory(
        diff_dir,
        missing_ok=True,
        missing_code="diff_dir_missing",
        unreadable_code="diff_dir_unreadable",
    )
    if diff_scan_error:
        _append_unique(errors, diff_scan_error)
        diff_workspace_safe = False
        path_graph_safe = False
        for case_id in case_errors:
            _add_case_error(case_errors, case_id, diff_scan_error)
    unexpected_diff_names = sorted(set(observed_diff_names) - set(expected_diff_names))
    if unexpected_diff_names:
        _append_unique(errors, "unexpected_diff_png_set")
        diff_workspace_safe = False
    for name, kind in diff_kinds.items():
        if kind == "regular":
            continue
        case_id = name.removesuffix(".diff.png") if name in set(expected_diff_names) else ""
        code = f"diff_{kind}_forbidden" if kind != "unreadable" else "diff_unreadable"
        _append_unique(errors, code)
        diff_workspace_safe = False
        path_graph_safe = False
        if case_id:
            _add_case_error(case_errors, case_id, code)

    canonical_paths: dict[str, str] = {}
    inode_keys: dict[str, tuple[int, int]] = {}
    node_by_label = {node.label: node for node in nodes}
    for node in nodes:
        try:
            canonical_paths[node.label] = str(node.path.resolve())
        except (OSError, RuntimeError) as exc:
            code = f"{node.role}_path_unresolvable"
            _append_unique(errors, code)
            path_graph_safe = False
            if node.case_id:
                _add_case_error(case_errors, node.case_id, code)
            if node.role == "receipt":
                receipt_write_allowed = False
            continue
        try:
            stat_result = os.lstat(node.path)
        except FileNotFoundError:
            continue
        except OSError:
            code = f"{node.role}_path_unreadable"
            _append_unique(errors, code)
            path_graph_safe = False
            if node.case_id:
                _add_case_error(case_errors, node.case_id, code)
            if node.role == "receipt":
                receipt_write_allowed = False
            continue
        if stat.S_ISLNK(stat_result.st_mode):
            code = f"{node.role}_symlink_forbidden"
            _append_unique(errors, code)
            path_graph_safe = False
            if node.case_id:
                _add_case_error(case_errors, node.case_id, code)
            if node.role == "receipt":
                receipt_write_allowed = False
        elif not stat.S_ISREG(stat_result.st_mode):
            code = f"{node.role}_non_regular_forbidden"
            _append_unique(errors, code)
            path_graph_safe = False
            if node.case_id:
                _add_case_error(case_errors, node.case_id, code)
            if node.role == "receipt":
                receipt_write_allowed = False
        try:
            followed = os.stat(node.path)
        except OSError:
            continue
        inode_keys[node.label] = (followed.st_dev, followed.st_ino)

    for first_index, first in enumerate(nodes):
        for second in nodes[first_index + 1 :]:
            same_path = (
                first.label in canonical_paths
                and second.label in canonical_paths
                and canonical_paths[first.label] == canonical_paths[second.label]
            )
            same_inode = (
                first.label in inode_keys
                and second.label in inode_keys
                and inode_keys[first.label] == inode_keys[second.label]
            )
            if not same_path and not same_inode:
                continue
            collision = f"path_graph_collision:{first.label}:{second.label}"
            _append_unique(errors, collision)
            path_graph_safe = False
            for node in (first, second):
                if node.case_id:
                    _add_case_error(case_errors, node.case_id, "path_graph_collision")
                if node.role == "receipt":
                    receipt_write_allowed = False
            roles = {first.role, second.role}
            if roles & {"manifest", "baseline"} and roles & {
                "actual",
                "diff",
                "receipt",
            }:
                protected_output_collision = True

    # Guard against a duplicate label silently hiding one graph node.
    if len(node_by_label) != len(nodes):
        _append_unique(errors, "path_graph_label_duplicate")
        path_graph_safe = False

    return VerificationPreflight(
        case_paths=case_paths,
        case_errors=case_errors,
        errors=errors,
        nodes=nodes,
        expected_actual_names=expected_actual_names,
        observed_actual_names=observed_actual_names,
        extra_actual_names=extra_actual_names,
        missing_actual_names=missing_actual_names,
        path_graph_safe=path_graph_safe,
        actual_workspace_safe=actual_workspace_safe,
        diff_workspace_safe=diff_workspace_safe,
        receipt_write_allowed=receipt_write_allowed,
        protected_output_collision=protected_output_collision,
    )


def _clean_expected_diffs(preflight: VerificationPreflight) -> None:
    for case_id, plan in preflight.case_paths.items():
        reasons = preflight.case_errors.get(case_id, [])
        if any(
            reason == "path_graph_collision" or reason.startswith("diff_")
            for reason in reasons
        ):
            continue
        try:
            stat_result = os.lstat(plan.diff_path)
        except FileNotFoundError:
            continue
        except OSError:
            _add_case_error(preflight.case_errors, case_id, "diff_cleanup_failed")
            _append_unique(preflight.errors, "diff_cleanup_failed")
            preflight.diff_workspace_safe = False
            continue
        if not stat.S_ISREG(stat_result.st_mode):
            _add_case_error(preflight.case_errors, case_id, "diff_cleanup_unsafe")
            _append_unique(preflight.errors, "diff_cleanup_unsafe")
            preflight.diff_workspace_safe = False
            continue
        try:
            plan.diff_path.unlink()
            _fsync_directory(plan.diff_path.parent)
        except OSError:
            _add_case_error(preflight.case_errors, case_id, "diff_cleanup_failed")
            _append_unique(preflight.errors, "diff_cleanup_failed")
            preflight.diff_workspace_safe = False


def _protected_fingerprints(preflight: VerificationPreflight) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for node in preflight.nodes:
        if node.role not in {"manifest", "baseline"}:
            continue
        limit = MAX_MANIFEST_BYTES if node.role == "manifest" else MAX_PNG_BYTES
        try:
            payload = _bounded_bytes(
                node.path,
                limit=limit,
                missing_code=f"{node.role}_missing",
                large_code=f"{node.role}_size_invalid",
            )
            fingerprints[node.label] = f"sha256:{_sha256(payload)}"
        except VisualBaselineError as exc:
            fingerprints[node.label] = f"error:{exc.code}"
    return fingerprints


def _dimension_diff(width: int, height: int) -> bytes:
    return encode_rgba_png(width, height, bytes((208, 30, 64, 255)) * (width * height))


def _empty_case_outcome(
    case: Mapping[str, Any], reasons: Sequence[str]
) -> dict[str, Any]:
    case_id = str(case["id"])
    return {
        "case_id": case_id,
        "status": "fail",
        "reasons": list(dict.fromkeys(reasons)),
        "baseline_path": str(case["baseline"]),
        "actual_path": f"{case_id}.png",
        "diff_path": "",
        "expected_dimensions": {
            "width": int(case["width"]),
            "height": int(case["height"]),
        },
        "baseline_dimensions": None,
        "actual_dimensions": None,
        "baseline_sha256": "",
        "expected_baseline_sha256": str(case["sha256"]),
        "actual_sha256": "",
        "diff_sha256": "",
        "changed_pixel_count": None,
        "total_pixel_count": None,
        "changed_pixel_ratio": None,
        "maximum_yiq_delta": None,
    }


def _verify_case(
    case: Mapping[str, Any],
    *,
    paths: CasePathPlan | None,
    initial_reasons: Sequence[str],
    pixel_threshold: float,
    max_changed_pixel_ratio: float,
) -> dict[str, Any]:
    case_id = str(case["id"])
    actual_name = f"{case_id}.png"
    diff_name = f"{case_id}.diff.png"
    reasons = list(dict.fromkeys(initial_reasons))
    if paths is None:
        reasons.append("path_plan_incomplete")
    if reasons:
        return _empty_case_outcome(case, reasons)
    assert paths is not None
    baseline_path = paths.baseline_path
    actual_path = paths.actual_path
    diff_path = paths.diff_path
    baseline_payload = b""
    actual_payload = b""
    baseline_sha = ""
    actual_sha = ""
    diff_sha = ""
    baseline_decoded: DecodedPNG | None = None
    actual_decoded: DecodedPNG | None = None
    comparison: PixelComparison | None = None
    try:
        baseline_payload = _bounded_bytes(
            baseline_path,
            limit=MAX_PNG_BYTES,
            missing_code="baseline_missing",
            large_code="baseline_size_invalid",
        )
        baseline_sha = _sha256(baseline_payload)
        if baseline_sha != case["sha256"]:
            reasons.append("baseline_sha256_mismatch")
        try:
            baseline_decoded = decode_png(baseline_payload)
        except VisualBaselineError as exc:
            reasons.append(f"baseline_{exc.code}")
    except VisualBaselineError as exc:
        reasons.append(exc.code)
    try:
        actual_payload = _bounded_bytes(
            actual_path,
            limit=MAX_PNG_BYTES,
            missing_code="actual_missing",
            large_code="actual_size_invalid",
        )
        actual_sha = _sha256(actual_payload)
        try:
            actual_decoded = decode_png(actual_payload)
        except VisualBaselineError as exc:
            reasons.append(f"actual_{exc.code}")
    except VisualBaselineError as exc:
        reasons.append(exc.code)

    expected_width = int(case["width"])
    expected_height = int(case["height"])
    if baseline_decoded is not None and (
        baseline_decoded.width != expected_width
        or baseline_decoded.height != expected_height
    ):
        reasons.append("baseline_dimension_mismatch")
    if actual_decoded is not None and (
        actual_decoded.width != expected_width
        or actual_decoded.height != expected_height
    ):
        reasons.append("actual_dimension_mismatch")
    if baseline_decoded is not None and actual_decoded is not None:
        if (
            baseline_decoded.width == actual_decoded.width
            and baseline_decoded.height == actual_decoded.height
        ):
            comparison = compare_pixels(
                baseline_decoded,
                actual_decoded,
                pixel_threshold=pixel_threshold,
            )
            try:
                _atomic_write(diff_path, comparison.diff_png, mode=0o600)
                diff_sha = _sha256(comparison.diff_png)
            except OSError:
                reasons.append("diff_write_failed")
            if comparison.changed_pixel_ratio > max_changed_pixel_ratio:
                reasons.append("changed_pixel_ratio_exceeded")
        elif baseline_decoded.width * baseline_decoded.height <= MAX_PIXELS:
            diff_payload = _dimension_diff(
                baseline_decoded.width, baseline_decoded.height
            )
            try:
                _atomic_write(diff_path, diff_payload, mode=0o600)
                diff_sha = _sha256(diff_payload)
            except OSError:
                reasons.append("diff_write_failed")

    return {
        "case_id": case_id,
        "status": "pass" if not reasons else "fail",
        "reasons": reasons,
        "baseline_path": str(case["baseline"]),
        "actual_path": actual_name,
        "diff_path": diff_name if diff_sha else "",
        "expected_dimensions": {"width": expected_width, "height": expected_height},
        "baseline_dimensions": (
            {"width": baseline_decoded.width, "height": baseline_decoded.height}
            if baseline_decoded is not None
            else None
        ),
        "actual_dimensions": (
            {"width": actual_decoded.width, "height": actual_decoded.height}
            if actual_decoded is not None
            else None
        ),
        "baseline_sha256": baseline_sha,
        "expected_baseline_sha256": str(case["sha256"]),
        "actual_sha256": actual_sha,
        "diff_sha256": diff_sha,
        "changed_pixel_count": (
            comparison.changed_pixel_count if comparison is not None else None
        ),
        "total_pixel_count": (
            comparison.total_pixel_count if comparison is not None else None
        ),
        "changed_pixel_ratio": (
            comparison.changed_pixel_ratio if comparison is not None else None
        ),
        "maximum_yiq_delta": (
            comparison.maximum_yiq_delta if comparison is not None else None
        ),
    }


def _valid_commit_sha(value: str) -> bool:
    return COMMIT_SHA_RE.fullmatch(str(value or "").strip().lower()) is not None


def verify_visual_baselines(
    *,
    manifest_path: Path,
    actual_dir: Path,
    diff_dir: Path,
    receipt_path: Path,
    release_commit_sha: str,
    expected_release_commit_sha: str,
    workflow_head_sha: str,
    source_binding_receipt: Mapping[str, Any],
    source_binding_receipt_sha256: str,
    browser_version: str,
    playwright_version: str,
) -> tuple[dict[str, Any], int]:
    release_sha = str(release_commit_sha or "").strip().lower()
    expected_sha = str(expected_release_commit_sha or "").strip().lower()
    manifest: dict[str, Any] | None = None
    manifest_payload = b""
    manifest_error = ""
    try:
        manifest, manifest_payload = load_manifest(manifest_path)
    except VisualBaselineError as exc:
        manifest_error = exc.code

    preflight_manifest: Mapping[str, Any] = (
        manifest if manifest is not None else {"cases": []}
    )
    preflight = _build_verification_preflight(
        manifest=preflight_manifest,
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )
    protected_before = _protected_fingerprints(preflight)
    _clean_expected_diffs(preflight)

    outcomes: list[dict[str, Any]] = []
    if manifest is not None:
        comparison_policy = dict(manifest["comparison"])
        for case in manifest["cases"]:
            case_id = str(case["id"])
            outcomes.append(
                _verify_case(
                    case,
                    paths=preflight.case_paths.get(case_id),
                    initial_reasons=preflight.case_errors.get(case_id, []),
                    pixel_threshold=float(comparison_policy["pixel_threshold"]),
                    max_changed_pixel_ratio=float(
                        comparison_policy["max_changed_pixel_ratio"]
                    ),
                )
            )
    protected_after = _protected_fingerprints(preflight)
    protected_inputs_unchanged = (
        protected_before == protected_after
        and not preflight.protected_output_collision
    )
    candidate_bound = (
        _valid_commit_sha(release_sha)
        and _valid_commit_sha(expected_sha)
        and release_sha == expected_sha
    )
    workflow_sha = str(workflow_head_sha or "").strip().lower()
    source_binding = dict(source_binding_receipt or {})
    source_binding_ok, source_binding_errors = validate_source_binding_receipt(
        source_binding,
        release_commit_sha=release_sha,
        workflow_head_sha=workflow_sha,
    )
    reported_source_binding_sha = str(
        source_binding_receipt_sha256 or ""
    ).strip().lower()
    try:
        expected_source_binding_sha = source_binding_payload_sha256(source_binding)
    except (TypeError, ValueError):
        expected_source_binding_sha = ""
    source_binding_digest_ok = bool(
        SHA256_RE.fullmatch(reported_source_binding_sha)
        and reported_source_binding_sha == expected_source_binding_sha
    )
    if not source_binding_digest_ok:
        source_binding_errors.append("source_binding_receipt_sha256_mismatch")
    browser_version_value = str(browser_version or "").strip()
    playwright_version_value = str(playwright_version or "").strip()
    browser_identity_complete = bool(browser_version_value and playwright_version_value)
    browser_fingerprint_payload = {
        "browser_engine": "chromium",
        "browser_version": browser_version_value,
        "playwright_version": playwright_version_value,
        "capture": dict(CAPTURE_CONTRACT),
    }
    browser_fingerprint = _sha256(
        json.dumps(
            browser_fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    expected_case_ids = (
        [str(case["id"]) for case in manifest["cases"]]
        if manifest is not None
        else []
    )
    observed_actual_set = set(preflight.observed_actual_names)
    observed_case_ids = (
        [
            str(case["id"])
            for case in manifest["cases"]
            if f"{case['id']}.png" in observed_actual_set
        ]
        if manifest is not None
        else []
    )
    observed_case_ids.extend(
        name[: -len(".png")]
        for name in preflight.observed_actual_names
        if name not in set(preflight.expected_actual_names)
    )
    actual_set_complete = (
        manifest is not None
        and preflight.actual_workspace_safe
        and preflight.observed_actual_names == sorted(preflight.expected_actual_names)
    )
    checks = [
        {"name": "candidate_sha_matches", "ok": candidate_bound},
        {
            "name": "source_checkout_bound",
            "ok": source_binding_ok and source_binding_digest_ok,
            "errors": source_binding_errors,
        },
        {"name": "manifest_schema_valid", "ok": manifest is not None, "error": manifest_error},
        {"name": "browser_identity_complete", "ok": browser_identity_complete},
        {
            "name": "path_graph_safe",
            "ok": manifest is not None and preflight.path_graph_safe,
        },
        {"name": "exact_actual_png_set", "ok": actual_set_complete},
        {
            "name": "diff_workspace_safe",
            "ok": manifest is not None and preflight.diff_workspace_safe,
        },
        {
            "name": "ordered_case_matrix_complete",
            "ok": bool(outcomes)
            and manifest is not None
            and len(outcomes) == len(manifest["cases"]),
            "expected_case_ids": expected_case_ids,
            "observed_case_ids": [outcome["case_id"] for outcome in outcomes],
        },
        {
            "name": "baseline_integrity_complete",
            "ok": bool(outcomes)
            and all(
                outcome["baseline_sha256"]
                == outcome["expected_baseline_sha256"]
                for outcome in outcomes
            ),
        },
        {
            "name": "exact_dimensions_complete",
            "ok": bool(outcomes)
            and all(
                outcome["baseline_dimensions"] == outcome["expected_dimensions"]
                and outcome["actual_dimensions"] == outcome["expected_dimensions"]
                for outcome in outcomes
            ),
        },
        {
            "name": "yiq_pixel_comparison_complete",
            "ok": bool(outcomes)
            and all(outcome["status"] == "pass" for outcome in outcomes),
        },
        {
            "name": "verify_did_not_update_baselines",
            "ok": protected_inputs_unchanged,
            "before": protected_before,
            "after": protected_after,
        },
        {
            "name": "receipt_path_safe",
            "ok": preflight.receipt_write_allowed,
        },
    ]
    passed = all(check["ok"] is True for check in checks)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if passed else "fail",
        "release_commit_sha": release_sha,
        "expected_release_commit_sha": expected_sha,
        "proof_mode": "chromium_screenshot_pixel_comparison",
        "screenshot_pixel_comparison": True,
        "update_mode": False,
        "receipt_written": preflight.receipt_write_allowed,
        "source_binding_receipt_sha256": reported_source_binding_sha,
        "source_binding": source_binding,
        "manifest": {
            "schema": MANIFEST_SCHEMA if manifest is not None else "",
            "sha256": _sha256(manifest_payload) if manifest_payload else "",
            "git_blob_sha1": _git_blob_sha1(manifest_payload) if manifest_payload else "",
            "case_count": len(manifest["cases"]) if manifest is not None else 0,
            "error": manifest_error,
        },
        "browser": {
            "name": "chromium",
            "version": browser_version_value,
            "playwright_version": playwright_version_value,
            "fingerprint_sha256": browser_fingerprint,
            "capture": dict(CAPTURE_CONTRACT),
        },
        "comparison": (
            dict(manifest["comparison"])
            if manifest is not None
            else {
                "algorithm": COMPARISON_ALGORITHM,
                "pixel_threshold": None,
                "max_changed_pixel_ratio": None,
            }
        ),
        "expected_case_ids": expected_case_ids,
        "observed_case_ids": observed_case_ids,
        "preflight": {
            "errors": preflight.errors,
            "expected_actual_pngs": sorted(preflight.expected_actual_names),
            "observed_actual_pngs": preflight.observed_actual_names,
            "missing_actual_pngs": preflight.missing_actual_names,
            "extra_actual_pngs": preflight.extra_actual_names,
            "path_graph_safe": preflight.path_graph_safe,
            "actual_workspace_safe": preflight.actual_workspace_safe,
            "diff_workspace_safe": preflight.diff_workspace_safe,
        },
        "outcome_count": len(outcomes),
        "failed_count": sum(outcome["status"] != "pass" for outcome in outcomes),
        "checks": checks,
        "outcomes": outcomes,
    }
    if not preflight.receipt_write_allowed:
        receipt["status"] = "fail"
        receipt["receipt_written"] = False
        return receipt, 1
    try:
        _atomic_write_json(receipt_path, receipt, mode=0o600)
    except OSError:
        receipt["status"] = "fail"
        receipt["receipt_written"] = False
        receipt["checks"].append(
            {"name": "receipt_written", "ok": False, "error": "receipt_write_failed"}
        )
        return receipt, 1
    return receipt, 0 if passed else 1


def _ci_truthy(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    false_values = {"", "0", "false", "no", "off"}
    return any(
        str(values.get(name, "")).strip().lower() not in false_values
        for name in CI_ENV_NAMES
    )


def update_baselines(
    *,
    manifest_path: Path,
    actual_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if _ci_truthy(environ):
        raise VisualBaselineError("baseline_update_forbidden_in_ci")
    manifest, _manifest_payload = load_manifest(manifest_path)
    expected_names = sorted(f"{case['id']}.png" for case in manifest["cases"])
    observed_names, entry_kinds, scan_error = _scan_png_directory(
        actual_dir,
        missing_ok=False,
        missing_code="actual_dir_missing",
        unreadable_code="actual_dir_unreadable",
    )
    if (
        scan_error
        or observed_names != expected_names
        or any(kind != "regular" for kind in entry_kinds.values())
    ):
        raise VisualBaselineError("actual_png_set_invalid")

    staged: list[tuple[dict[str, Any], Path, Path]] = []
    staged_bytes = 0
    try:
        for case in manifest["cases"]:
            actual_path = _safe_join(
                actual_dir,
                f"{case['id']}.png",
                code="actual_path_escape",
            )
            actual_payload = _bounded_bytes(
                actual_path,
                limit=MAX_PNG_BYTES,
                missing_code="actual_missing",
                large_code="actual_size_invalid",
            )
            staged_bytes += len(actual_payload)
            if staged_bytes > MAX_UPDATE_TOTAL_BYTES:
                raise VisualBaselineError("baseline_update_aggregate_size_exceeded")
            decoded = decode_png(actual_payload)
            if decoded.width != case["width"] or decoded.height != case["height"]:
                raise VisualBaselineError("actual_dimension_mismatch")
            baseline_path = _safe_join(
                manifest_path.parent,
                str(case["baseline"]),
                code="baseline_path_escape",
            )
            try:
                baseline_stat = os.lstat(baseline_path)
            except FileNotFoundError:
                baseline_stat = None
            except OSError as exc:
                raise VisualBaselineError("baseline_unreadable") from exc
            if baseline_stat is not None and stat.S_ISLNK(baseline_stat.st_mode):
                raise VisualBaselineError("baseline_symlink_forbidden")
            updated_case = dict(case)
            updated_case["sha256"] = _sha256(actual_payload)
            temporary_path = _stage_temporary(
                baseline_path,
                actual_payload,
                mode=0o644,
            )
            staged.append((updated_case, baseline_path, temporary_path))
        for _case, baseline_path, temporary_path in staged:
            _replace_staged(temporary_path, baseline_path, mode=0o644)
    finally:
        for _case, _baseline_path, temporary_path in staged:
            temporary_path.unlink(missing_ok=True)
    updated_manifest = dict(manifest)
    updated_manifest["cases"] = [case for case, _path, _temporary in staged]
    _atomic_write_json(manifest_path, updated_manifest, mode=0o644)
    return {
        "status": "updated",
        "schema": MANIFEST_SCHEMA,
        "case_count": len(staged),
        "manifest_sha256": _sha256(
            (json.dumps(updated_manifest, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        ),
    }


def _verify_command(args: argparse.Namespace) -> int:
    try:
        source_binding, source_binding_sha256 = load_source_binding_receipt(
            Path(args.source_binding_receipt)
        )
    except VisualBaselineError as exc:
        source_binding = {"load_error": exc.code}
        source_binding_sha256 = ""
    receipt, exit_code = verify_visual_baselines(
        manifest_path=Path(args.manifest),
        actual_dir=Path(args.actual_dir),
        diff_dir=Path(args.diff_dir),
        receipt_path=Path(args.receipt),
        release_commit_sha=args.release_sha,
        expected_release_commit_sha=args.expected_release_sha,
        workflow_head_sha=args.workflow_head_sha,
        source_binding_receipt=source_binding,
        source_binding_receipt_sha256=source_binding_sha256,
        browser_version=args.browser_version,
        playwright_version=args.playwright_version,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return exit_code


def _update_command(args: argparse.Namespace) -> int:
    try:
        result = update_baselines(
            manifest_path=Path(args.manifest),
            actual_dir=Path(args.actual_dir),
        )
    except VisualBaselineError as exc:
        print(json.dumps({"status": "fail", "error": exc.code}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify or explicitly update PropertyQuarry PNG screenshot baselines."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="Compare actual PNGs without changing baselines.")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--actual-dir", required=True)
    verify.add_argument("--diff-dir", required=True)
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--release-sha", required=True)
    verify.add_argument("--expected-release-sha", required=True)
    verify.add_argument("--workflow-head-sha", required=True)
    verify.add_argument("--source-binding-receipt", required=True)
    verify.add_argument("--browser-version", required=True)
    verify.add_argument("--playwright-version", required=True)
    verify.set_defaults(handler=_verify_command)
    update = subparsers.add_parser(
        "update", help="Explicitly replace existing baselines from captured actuals."
    )
    update.add_argument("--manifest", required=True)
    update.add_argument("--actual-dir", required=True)
    update.set_defaults(handler=_update_command)
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
