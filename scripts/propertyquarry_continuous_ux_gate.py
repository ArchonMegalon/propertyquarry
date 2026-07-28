#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import stat
import statistics
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.propertyquarry_playwright_runtime import (
    SUPPORTED_PLAYWRIGHT_ENGINES,
    normalize_playwright_engine,
    playwright_browser_type,
    playwright_engine_launch_kwargs,
)
from scripts.propertyquarry_visual_baseline import (
    RELEASE_METADATA_DESCENDANT_PATHS,
    SOURCE_BINDING_REQUIRED_CHECKS,
    SOURCE_BINDING_SCHEMA,
    source_binding_payload_sha256,
    validate_source_binding_receipt,
)


SCHEMA = "propertyquarry.continuous_ux_receipt.v2"
PROOF_SCOPE = "isolated_loopback_memory_app"
REAL_PROOF_MODE = "playwright_browser_all_isolated"
MOCK_PROOF_MODE = "contract_mock"
VISUAL_BASELINE_SCHEMA = "propertyquarry.visual_baseline_receipt.v1"
VISUAL_BASELINE_MANIFEST_SCHEMA = "propertyquarry.visual_baseline_manifest.v1"
VISUAL_BASELINE_PROOF_MODE = "chromium_screenshot_pixel_comparison"
VISUAL_BASELINE_ALGORITHM = "yiq-perceptual-rgba-on-white.v1"
VISUAL_BASELINE_REQUIRED_CASES = (
    ("public-home.desktop", 1440, 820),
    ("public-home.mobile", 430, 932),
    ("sign-in.desktop", 1440, 820),
    ("sign-in.mobile", 430, 932),
    ("search-setup.desktop", 1600, 1200),
    ("search-setup.mobile", 430, 932),
    ("results.desktop", 1440, 900),
    ("results.mobile", 390, 844),
    ("research-no-tour.mobile", 390, 844),
    ("empty-results.desktop", 1440, 900),
    ("offline.mobile", 390, 844),
)
VISUAL_BASELINE_REQUIRED_CASE_IDS = tuple(
    case_id for case_id, _width, _height in VISUAL_BASELINE_REQUIRED_CASES
)
VISUAL_BASELINE_CAPTURE_CONTRACT = {
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
VISUAL_BASELINE_REQUIRED_CHECKS = (
    "candidate_sha_matches",
    "source_checkout_bound",
    "manifest_schema_valid",
    "browser_identity_complete",
    "path_graph_safe",
    "exact_actual_png_set",
    "diff_workspace_safe",
    "ordered_case_matrix_complete",
    "baseline_integrity_complete",
    "exact_dimensions_complete",
    "yiq_pixel_comparison_complete",
    "verify_did_not_update_baselines",
    "receipt_path_safe",
)
VISUAL_BASELINE_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
FIRST_VALUE_BUDGET_MS = 3_200.0
FIRST_VALUE_BASIS = "median_three_warm_dom_content_loaded_visible_structure"
FIRST_VALUE_ENGINE = "chromium"
FIRST_VALUE_SAMPLE_COUNT = 3
FIRST_VALUE_MAX_ATTEMPTS = 2
VISIBLE_IMAGE_STABILITY_MS = 200
DEFAULT_ROUTES = (
    "/",
    "/app/search",
    "/app/search?continuous_ux_state=offline",
)
ERROR_ROUTE = "/app/search?continuous_ux_state=offline"
ERROR_STATE_KIND = "offline"
ERROR_EXPECTED_STATUS = 200
SEARCH_ROUTE = "/app/search"
REQUIRED_STATE_KINDS = ("loading", "error")
REQUIRED_ROW_CHECKS = (
    "route_document_loaded",
    "structural_visual_contract",
    "zoom_400_reflow",
    "first_value_under_budget",
    "provider_response_not_mocked",
)


def normalize_browser_engines(
    engines: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_engine in engines or SUPPORTED_PLAYWRIGHT_ENGINES:
        engine = normalize_playwright_engine(raw_engine)
        if engine not in normalized:
            normalized.append(engine)
    return tuple(normalized)


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _relative_route(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    return urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


def _redacted_browser_error_detail(
    exc: Exception,
    *,
    sensitive_values: tuple[str, ...] | list[str] = (),
    limit: int = 1_000,
) -> str:
    detail = re.sub(r"\s+", " ", str(exc or "")).strip()
    for sensitive in sorted(
        {str(value) for value in sensitive_values if str(value)},
        key=len,
        reverse=True,
    ):
        detail = detail.replace(sensitive, "[redacted]")
    return detail[: max(1, int(limit))]


def loopback_origin_error(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        return "invalid_url"
    host = str(parsed.hostname or "").strip().lower()
    if parsed.scheme != "http":
        return "http_loopback_required"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return "loopback_host_required"
    if parsed.username or parsed.password:
        return "userinfo_forbidden"
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return "origin_without_path_required"
    return ""


def _exact_json_object(raw: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError("duplicate_json_key")
            payload[key] = value
        return payload

    def reject_nonfinite_constant(_value: str) -> None:
        raise ValueError("nonfinite_json_number")

    parsed = json.loads(
        raw,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError("json_object_required")
    return parsed


def visual_baseline_payload_sha256(receipt: object) -> str:
    """Return the stable digest used to bind an embedded visual receipt."""

    payload = json.dumps(
        receipt,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_visual_baseline_receipt(path: Path) -> tuple[dict[str, Any], str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ValueError("visual_baseline_receipt_missing") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("visual_baseline_receipt_regular_file_required") from exc
        raise ValueError("visual_baseline_receipt_unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("visual_baseline_receipt_regular_file_required")
        if metadata.st_size < 2 or metadata.st_size > VISUAL_BASELINE_MAX_RECEIPT_BYTES:
            raise ValueError("visual_baseline_receipt_size_invalid")
        try:
            path_metadata_before = path.lstat()
        except OSError as exc:
            raise ValueError("visual_baseline_receipt_changed_during_read") from exc
        if (
            stat.S_ISLNK(path_metadata_before.st_mode)
            or not stat.S_ISREG(path_metadata_before.st_mode)
            or (path_metadata_before.st_dev, path_metadata_before.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError("visual_baseline_receipt_regular_file_required")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read(VISUAL_BASELINE_MAX_RECEIPT_BYTES + 1)
        try:
            path_metadata_after = path.lstat()
        except OSError as exc:
            raise ValueError("visual_baseline_receipt_changed_during_read") from exc
        if (
            stat.S_ISLNK(path_metadata_after.st_mode)
            or not stat.S_ISREG(path_metadata_after.st_mode)
            or (path_metadata_after.st_dev, path_metadata_after.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or path_metadata_after.st_size != metadata.st_size
            or len(payload) != metadata.st_size
        ):
            raise ValueError("visual_baseline_receipt_changed_during_read")
    except OSError as exc:
        raise ValueError("visual_baseline_receipt_unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        receipt = _exact_json_object(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("visual_baseline_receipt_json_invalid") from exc
    return receipt, visual_baseline_payload_sha256(receipt)


def validate_visual_baseline_receipt(
    receipt: object,
    *,
    expected_release_commit_sha: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return False, ["receipt_object_required"]
    required_top_keys = {
        "schema",
        "generated_at",
        "status",
        "release_commit_sha",
        "expected_release_commit_sha",
        "proof_mode",
        "screenshot_pixel_comparison",
        "update_mode",
        "receipt_written",
        "source_binding_receipt_sha256",
        "source_binding",
        "manifest",
        "browser",
        "comparison",
        "expected_case_ids",
        "observed_case_ids",
        "preflight",
        "outcome_count",
        "failed_count",
        "checks",
        "outcomes",
    }
    if set(receipt) != required_top_keys:
        errors.append("receipt_keys_invalid")
    expected_sha = (
        expected_release_commit_sha.strip().lower()
        if type(expected_release_commit_sha) is str
        else ""
    )
    reported_sha = (
        receipt.get("release_commit_sha")
        if type(receipt.get("release_commit_sha")) is str
        else ""
    )
    reported_expected_sha = (
        receipt.get("expected_release_commit_sha")
        if type(receipt.get("expected_release_commit_sha")) is str
        else ""
    )
    if receipt.get("schema") != VISUAL_BASELINE_SCHEMA:
        errors.append("schema_mismatch")
    if receipt.get("status") != "pass":
        errors.append("status_not_pass")
    if receipt.get("proof_mode") != VISUAL_BASELINE_PROOF_MODE:
        errors.append("proof_mode_mismatch")
    if receipt.get("screenshot_pixel_comparison") is not True:
        errors.append("pixel_comparison_not_true")
    if receipt.get("update_mode") is not False:
        errors.append("update_mode_must_be_false")
    if receipt.get("receipt_written") is not True:
        errors.append("receipt_not_written")
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
        errors.append("generated_at_invalid")
    if (
        re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None
        or reported_sha != expected_sha
        or reported_expected_sha != expected_sha
    ):
        errors.append("release_commit_sha_mismatch")

    source_binding = receipt.get("source_binding")
    workflow_head_sha = (
        source_binding.get("head_commit")
        if isinstance(source_binding, dict)
        and type(source_binding.get("head_commit")) is str
        else ""
    )
    source_binding_ok, source_binding_errors = validate_source_binding_receipt(
        source_binding,
        release_commit_sha=expected_sha,
        workflow_head_sha=workflow_head_sha,
    )
    reported_source_binding_sha = receipt.get("source_binding_receipt_sha256")
    try:
        expected_source_binding_sha = source_binding_payload_sha256(source_binding)
    except (TypeError, ValueError):
        expected_source_binding_sha = ""
    if (
        type(reported_source_binding_sha) is not str
        or re.fullmatch(r"[0-9a-f]{64}", reported_source_binding_sha) is None
        or reported_source_binding_sha != expected_source_binding_sha
    ):
        source_binding_errors.append("source_binding_receipt_sha256_mismatch")
        source_binding_ok = False
    if not source_binding_ok:
        errors.extend(source_binding_errors)

    expected_case_ids = list(VISUAL_BASELINE_REQUIRED_CASE_IDS)
    if receipt.get("expected_case_ids") != expected_case_ids:
        errors.append("expected_case_matrix_mismatch")
    if receipt.get("observed_case_ids") != expected_case_ids:
        errors.append("observed_case_matrix_mismatch")
    outcome_count = receipt.get("outcome_count", -1)
    failed_count = receipt.get("failed_count", -1)
    if outcome_count != len(expected_case_ids) or failed_count != 0:
        errors.append("outcome_counts_invalid")
    if type(outcome_count) is not int or type(failed_count) is not int:
        errors.append("outcome_count_types_invalid")

    manifest = receipt.get("manifest")
    if not isinstance(manifest, dict):
        errors.append("manifest_evidence_missing")
    else:
        if set(manifest) != {
            "schema",
            "sha256",
            "git_blob_sha1",
            "case_count",
            "error",
        }:
            errors.append("manifest_keys_invalid")
        if manifest.get("schema") != VISUAL_BASELINE_MANIFEST_SCHEMA:
            errors.append("manifest_schema_mismatch")
        if (
            type(manifest.get("case_count")) is not int
            or manifest.get("case_count") != len(expected_case_ids)
        ):
            errors.append("manifest_case_count_mismatch")
        if (
            type(manifest.get("sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", manifest.get("sha256")) is None
        ):
            errors.append("manifest_sha256_invalid")
        if (
            type(manifest.get("git_blob_sha1")) is not str
            or re.fullmatch(r"[0-9a-f]{40}", manifest.get("git_blob_sha1"))
            is None
        ):
            errors.append("manifest_git_blob_invalid")
        if manifest.get("error") != "":
            errors.append("manifest_error_present")

    browser = receipt.get("browser")
    if not isinstance(browser, dict):
        errors.append("browser_identity_missing")
    else:
        if set(browser) != {
            "name",
            "version",
            "playwright_version",
            "fingerprint_sha256",
            "capture",
        }:
            errors.append("browser_keys_invalid")
        if browser.get("name") != "chromium":
            errors.append("browser_name_mismatch")
        browser_version = browser.get("version")
        playwright_version = browser.get("playwright_version")
        if type(browser_version) is not str or not browser_version.strip():
            errors.append("browser_version_missing")
        if type(playwright_version) is not str or not playwright_version.strip():
            errors.append("playwright_version_missing")
        capture = browser.get("capture")
        if not isinstance(capture, dict) or capture != VISUAL_BASELINE_CAPTURE_CONTRACT:
            errors.append("capture_contract_invalid")
        elif any(
            type(capture[key]) is not type(expected)
            for key, expected in VISUAL_BASELINE_CAPTURE_CONTRACT.items()
        ):
            errors.append("capture_contract_types_invalid")
        try:
            expected_browser_fingerprint = visual_baseline_payload_sha256(
                {
                    "browser_engine": "chromium",
                    "browser_version": browser_version,
                    "playwright_version": playwright_version,
                    "capture": capture,
                }
            )
        except (TypeError, ValueError):
            expected_browser_fingerprint = ""
        if (
            type(browser.get("fingerprint_sha256")) is not str
            or browser.get("fingerprint_sha256") != expected_browser_fingerprint
        ):
            errors.append("browser_fingerprint_invalid")

    pixel_threshold = -1.0
    changed_ratio = -1.0
    comparison = receipt.get("comparison")
    if not isinstance(comparison, dict):
        errors.append("comparison_evidence_missing")
    else:
        if set(comparison) != {
            "algorithm",
            "pixel_threshold",
            "max_changed_pixel_ratio",
        }:
            errors.append("comparison_keys_invalid")
        if comparison.get("algorithm") != VISUAL_BASELINE_ALGORITHM:
            errors.append("comparison_algorithm_mismatch")
        raw_pixel_threshold = comparison.get("pixel_threshold")
        raw_changed_ratio = comparison.get("max_changed_pixel_ratio")
        pixel_threshold = (
            float(raw_pixel_threshold)
            if type(raw_pixel_threshold) in {int, float}
            else -1.0
        )
        changed_ratio = (
            float(raw_changed_ratio)
            if type(raw_changed_ratio) in {int, float}
            else -1.0
        )
        if not (
            math.isfinite(pixel_threshold)
            and pixel_threshold == 0.1
            and math.isfinite(changed_ratio)
            and changed_ratio == 0.005
        ):
            errors.append("comparison_thresholds_invalid")

    raw_checks_value = receipt.get("checks")
    raw_checks = (
        [dict(check) for check in raw_checks_value if isinstance(check, dict)]
        if isinstance(raw_checks_value, list)
        else []
    )
    check_names = [
        check.get("name") if type(check.get("name")) is str else ""
        for check in raw_checks
    ]
    if (
        len(raw_checks) != len(VISUAL_BASELINE_REQUIRED_CHECKS)
        or check_names != list(VISUAL_BASELINE_REQUIRED_CHECKS)
        or any(check.get("ok") is not True for check in raw_checks)
    ):
        errors.append("verification_checks_incomplete")

    expected_actual_pngs = sorted(f"{case_id}.png" for case_id in expected_case_ids)
    preflight = receipt.get("preflight")
    if not isinstance(preflight, dict):
        errors.append("preflight_evidence_missing")
    else:
        if set(preflight) != {
            "errors",
            "expected_actual_pngs",
            "observed_actual_pngs",
            "missing_actual_pngs",
            "extra_actual_pngs",
            "path_graph_safe",
            "actual_workspace_safe",
            "diff_workspace_safe",
        }:
            errors.append("preflight_keys_invalid")
        if preflight.get("errors") != []:
            errors.append("preflight_errors_present")
        if preflight.get("expected_actual_pngs") != expected_actual_pngs:
            errors.append("preflight_expected_actual_set_mismatch")
        if preflight.get("observed_actual_pngs") != expected_actual_pngs:
            errors.append("preflight_observed_actual_set_mismatch")
        if preflight.get("missing_actual_pngs") != []:
            errors.append("preflight_actuals_missing")
        if preflight.get("extra_actual_pngs") != []:
            errors.append("preflight_actuals_extra")
        if any(
            preflight.get(field) is not True
            for field in (
                "path_graph_safe",
                "actual_workspace_safe",
                "diff_workspace_safe",
            )
        ):
            errors.append("preflight_workspace_unsafe")

    raw_outcomes = receipt.get("outcomes")
    outcomes = (
        [dict(outcome) for outcome in raw_outcomes if isinstance(outcome, dict)]
        if isinstance(raw_outcomes, list)
        else []
    )
    if [str(outcome.get("case_id") or "") for outcome in outcomes] != expected_case_ids:
        errors.append("outcome_case_matrix_mismatch")
    required_outcome_keys = {
        "case_id",
        "status",
        "reasons",
        "baseline_path",
        "actual_path",
        "diff_path",
        "expected_dimensions",
        "baseline_dimensions",
        "actual_dimensions",
        "baseline_sha256",
        "expected_baseline_sha256",
        "actual_sha256",
        "diff_sha256",
        "changed_pixel_count",
        "total_pixel_count",
        "changed_pixel_ratio",
        "maximum_yiq_delta",
    }
    for outcome, (case_id, width, height) in zip(
        outcomes,
        VISUAL_BASELINE_REQUIRED_CASES,
    ):
        expected_dimensions = {"width": width, "height": height}
        if set(outcome) != required_outcome_keys:
            errors.append("outcome_keys_invalid")
        if outcome.get("status") != "pass" or outcome.get("reasons") != []:
            errors.append("outcome_failure_present")
        if outcome.get("baseline_path") != f"images/{case_id}.png":
            errors.append("baseline_path_mismatch")
        if outcome.get("actual_path") != f"{case_id}.png":
            errors.append("actual_path_mismatch")
        if outcome.get("diff_path") != f"{case_id}.diff.png":
            errors.append("diff_path_mismatch")
        if outcome.get("expected_dimensions") != expected_dimensions:
            errors.append("expected_dimensions_mismatch")
        if outcome.get("baseline_dimensions") != expected_dimensions:
            errors.append("baseline_dimensions_mismatch")
        if outcome.get("actual_dimensions") != expected_dimensions:
            errors.append("actual_dimensions_mismatch")
        baseline_sha = outcome.get("baseline_sha256")
        expected_baseline_sha = outcome.get("expected_baseline_sha256")
        actual_sha = outcome.get("actual_sha256")
        diff_sha = outcome.get("diff_sha256")
        if (
            type(baseline_sha) is not str
            or re.fullmatch(r"[0-9a-f]{64}", baseline_sha) is None
            or baseline_sha != expected_baseline_sha
        ):
            errors.append("baseline_integrity_mismatch")
        if (
            type(actual_sha) is not str
            or re.fullmatch(r"[0-9a-f]{64}", actual_sha) is None
        ):
            errors.append("actual_sha256_invalid")
        if (
            type(diff_sha) is not str
            or re.fullmatch(r"[0-9a-f]{64}", diff_sha) is None
        ):
            errors.append("diff_sha256_invalid")
        changed_count = outcome.get("changed_pixel_count")
        total_count = outcome.get("total_pixel_count")
        raw_outcome_ratio = outcome.get("changed_pixel_ratio")
        maximum_delta = outcome.get("maximum_yiq_delta")
        if (
            type(changed_count) is not int
            or type(total_count) is not int
            or total_count != width * height
            or not 0 <= changed_count <= total_count
        ):
            errors.append("pixel_counts_invalid")
        if type(raw_outcome_ratio) not in {int, float}:
            outcome_ratio = -1.0
        else:
            outcome_ratio = float(raw_outcome_ratio)
        if (
            not math.isfinite(outcome_ratio)
            or outcome_ratio < 0.0
            or outcome_ratio > changed_ratio
            or (
                type(changed_count) is int
                and type(total_count) is int
                and total_count > 0
                and not math.isclose(
                    outcome_ratio,
                    changed_count / total_count,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            )
        ):
            errors.append("changed_pixel_ratio_invalid")
        if (
            type(maximum_delta) not in {int, float}
            or not math.isfinite(float(maximum_delta))
            or not 0.0 <= float(maximum_delta) <= 1.000001
        ):
            errors.append("maximum_yiq_delta_invalid")
    return not errors, list(dict.fromkeys(errors))


def _continue_with_origin_scoped_headers(
    route: Any,
    *,
    authorized_origin: str,
    headers: dict[str, str],
    auth_enabled: dict[str, bool],
    routing_evidence: dict[str, int] | None = None,
) -> None:
    if routing_evidence is not None:
        routing_evidence["continued_request_count"] = (
            int(routing_evidence.get("continued_request_count") or 0) + 1
        )
    if (
        _origin(str(route.request.url or "")) != authorized_origin
        or not auth_enabled["value"]
    ):
        route.continue_()
        return
    merged = dict(route.request.headers)
    merged.update(headers)
    route.continue_(headers=merged)


def _wait_for_visible_image_terminal_state(page: Any, *, timeout_ms: int) -> None:
    page.evaluate(
        """
        async ({ timeoutMs, stabilityMs }) => {
          const visible = (image) => {
            const style = getComputedStyle(image);
            const rect = image.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && rect.width > 0 && rect.height > 0;
          };
          const sourceToken = (image) => {
            const current = String(image.currentSrc || '').trim();
            if (current) return `current:${current}`;
            const declared = String(image.getAttribute('src') || '').trim();
            if (declared) return `src:${declared}`;
            const srcset = String(image.getAttribute('srcset') || '').trim();
            if (srcset) return `srcset:${srcset}`;
            const picture = image.closest('picture');
            const pictureSources = picture
              ? Array.from(picture.querySelectorAll('source'))
                .map((source) => String(source.getAttribute('srcset') || '').trim())
                .filter(Boolean)
                .join('|')
              : '';
            return pictureSources ? `picture:${pictureSources}` : '';
          };
          const imageIds = new WeakMap();
          let nextImageId = 1;
          const imageId = (image) => {
            if (!imageIds.has(image)) imageIds.set(image, nextImageId++);
            return imageIds.get(image);
          };
          const snapshot = () => Array.from(document.images)
            .filter(visible)
            .map((image) => ({
              image,
              id: imageId(image),
              source: sourceToken(image),
              complete: image.complete,
            }));
          const signature = (rows) => JSON.stringify(
            rows.map((row) => [row.id, row.source])
          );
          const deadline = performance.now() + timeoutMs;
          const nextFrame = () => new Promise((resolve) => {
            const remainingMs = Math.max(0, deadline - performance.now());
            if (remainingMs <= 0) {
              resolve(false);
              return;
            }
            let settled = false;
            const timer = setTimeout(() => {
              if (settled) return;
              settled = true;
              resolve(false);
            }, Math.min(100, remainingMs));
            requestAnimationFrame(() => {
              if (settled) return;
              settled = true;
              clearTimeout(timer);
              resolve(true);
            });
          });
          let priorSignature = null;
          let stableFrames = 0;
          let stableSince = null;

          while (performance.now() < deadline) {
            const rows = snapshot();
            const ready = rows.every((row) => Boolean(row.source) && row.complete);
            const currentSignature = ready ? signature(rows) : null;
            if (ready && currentSignature === priorSignature) {
              stableFrames += 1;
            } else {
              stableFrames = ready ? 1 : 0;
              priorSignature = currentSignature;
              stableSince = ready ? performance.now() : null;
            }
            if (
              ready
              && stableFrames >= 2
              && stableSince !== null
              && performance.now() - stableSince >= stabilityMs
            ) {
              const remainingMs = Math.max(1, deadline - performance.now());
              const decodeBudgetMs = Math.min(1_000, Math.max(1, remainingMs / 2));
              await Promise.race([
                Promise.allSettled(rows.map(({ image }) => (
                  typeof image.decode === 'function' ? image.decode() : Promise.resolve()
                ))),
                new Promise((resolve) => setTimeout(resolve, decodeBudgetMs)),
              ]);
              await nextFrame();
              await nextFrame();
              if (performance.now() >= deadline) break;
              const finalRows = snapshot();
              if (
                finalRows.every((row) => Boolean(row.source) && row.complete)
                && signature(finalRows) === currentSignature
              ) {
                return;
              }
              priorSignature = null;
              stableFrames = 0;
              stableSince = null;
            }
            if (!(await nextFrame())) {
              priorSignature = null;
              stableFrames = 0;
              stableSince = null;
            }
          }
          throw new Error('visible_image_terminal_state_timeout');
        }
        """,
        {
            "timeoutMs": max(1_000, min(int(timeout_ms), 10_000)),
            "stabilityMs": VISIBLE_IMAGE_STABILITY_MS,
        },
    )


def _structural_visual_metrics(page: Any) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """
            () => {
              const visible = (node) => {
                if (!node) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const root = document.documentElement;
              const main = document.querySelector('main');
              const interactive = Array.from(document.querySelectorAll(
                'a[href], button, input, select, textarea, summary, [role="button"]'
              )).filter(visible);
              const visibleImages = Array.from(document.images).filter(visible);
              const brokenImages = visibleImages.filter((image) => (
                !image.complete || image.naturalWidth <= 0
              ));
              const nav = document.querySelector(
                'nav[aria-label], [data-property-research-topnav], .pqx-topbar, .prd-topbar'
              );
              const bodyText = String(document.body && document.body.innerText || '').trim();
              const navigation = performance.getEntriesByType('navigation')[0];
              const paints = performance.getEntriesByType('paint');
              const firstContentfulPaint = paints.find((entry) => entry.name === 'first-contentful-paint');
              const domContentLoadedMs = Number(navigation && navigation.domContentLoadedEventEnd || 0);
              const firstContentfulPaintMs = Number(firstContentfulPaint && firstContentfulPaint.startTime || 0);
              return {
                document_ready_state: String(document.readyState || ''),
                main_visible: visible(main),
                navigation_visible: visible(nav),
                body_text_length: bodyText.length,
                body_scroll_width: Number(root.scrollWidth || 0),
                viewport_width: Number(root.clientWidth || window.innerWidth || 0),
                horizontal_overflow: Number(root.scrollWidth || 0) > Number(root.clientWidth || 0) + 2,
                visible_interactive_count: interactive.length,
                visible_image_count: visibleImages.length,
                terminal_visible_image_count: visibleImages.filter((image) => image.complete).length,
                broken_visible_image_count: brokenImages.length,
                dom_content_loaded_ms: domContentLoadedMs,
                first_contentful_paint_ms: firstContentfulPaintMs,
                first_value_ms: domContentLoadedMs,
                first_value_basis: 'dom_content_loaded_visible_structure_sample',
              };
            }
            """
        )
        or {}
    )


def _zoom_400_metrics(page: Any) -> dict[str, Any]:
    page.set_viewport_size({"width": 320, "height": 900})
    page.evaluate(
        """
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
        """
    )
    return dict(
        page.evaluate(
            """
            () => {
              const visible = (node) => {
                if (node.closest('[hidden], [aria-hidden="true"], details:not([open])')) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const insideHorizontalScrollRegion = (node) => {
                let current = node.parentElement;
                while (current && current !== document.body) {
                  const style = getComputedStyle(current);
                  if (['auto', 'scroll'].includes(style.overflowX)
                      && current.scrollWidth > current.clientWidth + 2) return true;
                  current = current.parentElement;
                }
                return false;
              };
              const width = Number(document.documentElement.clientWidth || 0);
              const interactive = Array.from(document.querySelectorAll(
                'a[href], button, input, select, textarea, summary, [role="button"]'
              )).filter(visible);
              const clipped = interactive.filter((node) => {
                const rect = node.getBoundingClientRect();
                return (rect.left < -2 || rect.right > width + 2)
                  && !insideHorizontalScrollRegion(node);
              });
              return {
                zoom_400_percent: 400,
                zoom_400_viewport_width: width,
                zoom_400_scroll_width: Number(document.documentElement.scrollWidth || 0),
                zoom_400_reflow_without_horizontal_scroll:
                  Number(document.documentElement.scrollWidth || 0) <= width + 2,
                zoom_400_clipped_interactive_count: clipped.length,
              };
            }
            """
        )
        or {}
    )


def _error_state_metrics(page: Any) -> dict[str, Any]:
    return dict(
        page.evaluate(
            """
            () => {
              const marker = document.querySelector('[data-pq-failure-state="offline"]');
              if (!marker) return { visible: false, semantic: false };
              const style = getComputedStyle(marker);
              const rect = marker.getBoundingClientRect();
              const visible = style.display !== 'none' && style.visibility !== 'hidden'
                && rect.width > 0 && rect.height > 0;
              return {
                visible,
                semantic: marker.getAttribute('role') === 'alert'
                  || marker.getAttribute('role') === 'status'
                  || ['polite', 'assertive'].includes(String(marker.getAttribute('aria-live') || '')),
              };
            }
            """
        )
        or {}
    )


def _loading_state_metrics(page: Any) -> dict[str, Any]:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.evaluate(
        """
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
        """
    )
    return dict(
        page.evaluate(
            """
            () => {
              const visible = (node) => {
                if (!node) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const form = document.querySelector('[data-console-form-variant="property_search"]');
              const button = document.querySelector('[data-property-start-top]')
                || (form && form.querySelector('button[type="submit"], input[type="submit"]'));
              if (!button || !visible(button) || button.disabled) {
                return {
                  action_available: false,
                  state_visible: false,
                  semantic: false,
                };
              }
              button.click();
              const candidates = Array.from(document.querySelectorAll(
                '[data-pq-failure-state="loading"], [aria-busy="true"], [role="status"], [aria-live]'
              )).filter(visible);
              const loadingWords = /searching|loading|finding|checking|gathering|working|preparing/i;
              const marker = candidates.find((node) => (
                node.getAttribute('data-pq-failure-state') === 'loading'
                || node.getAttribute('aria-busy') === 'true'
                || loadingWords.test(String(node.textContent || ''))
              ));
              const buttonText = String(button.textContent || button.value || '');
              const buttonLoading = Boolean(button.disabled || button.getAttribute('aria-busy') === 'true'
                || loadingWords.test(buttonText));
              const semantic = Boolean(marker && (
                marker.getAttribute('role') === 'status'
                || marker.getAttribute('role') === 'progressbar'
                || marker.getAttribute('aria-busy') === 'true'
                || ['polite', 'assertive'].includes(String(marker.getAttribute('aria-live') || ''))
              )) || button.getAttribute('aria-busy') === 'true';
              return {
                action_available: true,
                state_visible: Boolean(marker || buttonLoading),
                semantic,
              };
            }
            """
        )
        or {}
    )


def collect_continuous_ux_engine_rows(
    *,
    base_url: str,
    browser_engine: str,
    routes: tuple[str, ...],
    headers: dict[str, str],
    timeout_ms: int,
    first_value_budget_ms: float = FIRST_VALUE_BUDGET_MS,
) -> list[dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    engine = normalize_playwright_engine(browser_engine)
    normalized_base = str(base_url or "").rstrip("/")
    authorized_origin = _origin(normalized_base)
    rows: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser_type = playwright_browser_type(playwright, engine=engine)
        browser = browser_type.launch(
            **playwright_engine_launch_kwargs(
                playwright,
                engine=engine,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        )
        try:
            auth_enabled = {"value": True}
            routing_evidence = {
                "continued_request_count": 0,
                "route_fulfill_count": 0,
            }

            def new_isolated_context() -> Any:
                isolated_context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    service_workers="block",
                )
                isolated_context.route(
                    "**/*",
                    lambda route: _continue_with_origin_scoped_headers(
                        route,
                        authorized_origin=authorized_origin,
                        headers=headers,
                        auth_enabled=auth_enabled,
                        routing_evidence=routing_evidence,
                    ),
                )
                return isolated_context

            context = new_isolated_context()
            try:
                for route_index, route in enumerate(routes):
                    auth_enabled["value"] = route != "/"
                    routing_evidence["continued_request_count"] = 0
                    routing_evidence["route_fulfill_count"] = 0
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.set_default_navigation_timeout(timeout_ms)
                    status_code = 0
                    metrics: dict[str, Any] = {}
                    error = ""
                    error_type = ""
                    error_detail = ""
                    observation_stage = "route_navigation"
                    try:
                        response = page.goto(
                            normalized_base + "/" + route.lstrip("/"),
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                        status_code = int(response.status) if response is not None else 0
                        observation_stage = "body_visible"
                        page.locator("body").wait_for(state="visible", timeout=timeout_ms)
                        observation_stage = "visible_images_terminal"
                        _wait_for_visible_image_terminal_state(
                            page,
                            timeout_ms=timeout_ms,
                        )
                        observation_stage = "structural_visual_metrics"
                        visual_metrics = _structural_visual_metrics(page)
                        first_value_cold_ms = float(
                            visual_metrics.get("first_value_ms") or 0.0
                        )
                        first_value_samples = [first_value_cold_ms]
                        first_value_initial_samples = list(first_value_samples)
                        first_value_retry_used = False
                        if engine == FIRST_VALUE_ENGINE:
                            first_value_samples = []
                            for attempt_index in range(FIRST_VALUE_MAX_ATTEMPTS):
                                attempt_samples: list[float] = []
                                for sample_index in range(FIRST_VALUE_SAMPLE_COUNT):
                                    observation_stage = (
                                        "first_value_reload_"
                                        f"{attempt_index + 1}_{sample_index + 1}"
                                    )
                                    response = page.reload(
                                        wait_until="domcontentloaded",
                                        timeout=timeout_ms,
                                    )
                                    status_code = int(response.status) if response is not None else 0
                                    page.locator("body").wait_for(
                                        state="visible",
                                        timeout=timeout_ms,
                                    )
                                    _wait_for_visible_image_terminal_state(
                                        page,
                                        timeout_ms=timeout_ms,
                                    )
                                    visual_metrics = _structural_visual_metrics(page)
                                    attempt_samples.append(
                                        float(visual_metrics.get("first_value_ms") or 0.0)
                                    )
                                if attempt_index == 0:
                                    first_value_initial_samples = list(attempt_samples)
                                first_value_samples = attempt_samples
                                if (
                                    statistics.median(first_value_samples)
                                    <= float(first_value_budget_ms)
                                ):
                                    break
                                first_value_retry_used = True
                        metrics.update(visual_metrics)
                        metrics.update(
                            {
                                "first_value_ms": float(
                                    statistics.median(first_value_samples)
                                ),
                                "first_value_samples_ms": first_value_samples,
                                "first_value_initial_samples_ms": first_value_initial_samples,
                                "first_value_sample_count": len(first_value_samples),
                                "first_value_cold_ms": first_value_cold_ms,
                                "first_value_retry_used": first_value_retry_used,
                                "first_value_basis": FIRST_VALUE_BASIS,
                                "first_value_gated": engine == FIRST_VALUE_ENGINE,
                                "final_route": _relative_route(str(page.url or "")),
                            }
                        )
                        observation_stage = "zoom_400_metrics"
                        metrics.update(_zoom_400_metrics(page))
                        if route == ERROR_ROUTE:
                            observation_stage = "offline_transition"
                            context.set_offline(True)
                            try:
                                observation_stage = "offline_event"
                                page.evaluate("window.dispatchEvent(new Event('offline'))")
                                page.wait_for_timeout(150)
                                observation_stage = "offline_state_metrics"
                                error_state = _error_state_metrics(page)
                                metrics.update(
                                    {
                                        "error_state_kind": ERROR_STATE_KIND,
                                        "error_state_visible": error_state.get("visible") is True,
                                        "error_state_semantic": error_state.get("semantic") is True,
                                    }
                                )
                            finally:
                                context.set_offline(False)
                            observation_stage = "online_event"
                            page.evaluate("window.dispatchEvent(new Event('online'))")
                            page.wait_for_timeout(100)
                            observation_stage = "online_recovery_metrics"
                            recovered_state = _error_state_metrics(page)
                            metrics["error_state_recovered_online"] = (
                                recovered_state.get("visible") is False
                            )
                        if route == SEARCH_ROUTE:
                            observation_stage = "loading_state_metrics"
                            loading_state = _loading_state_metrics(page)
                            metrics.update(
                                {
                                    "loading_action_available": loading_state.get("action_available") is True,
                                    "loading_state_visible": loading_state.get("state_visible") is True,
                                    "loading_state_semantic": loading_state.get("semantic") is True,
                                }
                            )
                        observation_stage = "routing_evidence"
                        route_fulfill_count = int(
                            routing_evidence.get("route_fulfill_count") or 0
                        )
                        metrics.update(
                            {
                                "request_interception_mode": "origin_scoped_headers_continue_only",
                                "continued_request_count": int(
                                    routing_evidence.get("continued_request_count") or 0
                                ),
                                "route_fulfill_count": route_fulfill_count,
                                "provider_response_mocked": route_fulfill_count > 0,
                            }
                        )
                    except Exception as exc:
                        error = "browser_observation_failed"
                        error_type = type(exc).__name__
                        error_detail = _redacted_browser_error_detail(
                            exc,
                            sensitive_values=list(headers.values()),
                        )
                    row = {
                        "route": route,
                        "browser_engine": engine,
                        "status_code": status_code,
                        "metrics": metrics,
                        "error": error,
                        "error_type": error_type,
                        "error_stage": observation_stage if error else "",
                        "error_detail": error_detail,
                    }
                    checks = evaluate_continuous_ux_row(row)
                    row["checks"] = checks
                    row["ok"] = all(check.get("ok") is True for check in checks)
                    rows.append(row)
                    page.close()
                    if route_index + 1 < len(routes):
                        context.close()
                        context = new_isolated_context()
            finally:
                context.close()
        finally:
            browser.close()
    return rows


def evaluate_continuous_ux_row(
    row: dict[str, Any],
    *,
    first_value_budget_ms: float = FIRST_VALUE_BUDGET_MS,
) -> list[dict[str, Any]]:
    route = str(row.get("route") or "")
    metrics = dict(row.get("metrics") or {})
    browser_engine = str(row.get("browser_engine") or "").strip().lower()
    try:
        status_code = int(row["status_code"])
        body_text_length = int(metrics["body_text_length"])
        visible_interactive_count = int(metrics["visible_interactive_count"])
        visible_image_count = int(metrics["visible_image_count"])
        terminal_visible_image_count = int(metrics["terminal_visible_image_count"])
        broken_visible_image_count = int(metrics["broken_visible_image_count"])
        zoom_400_percent = int(metrics["zoom_400_percent"])
        zoom_400_viewport_width = int(metrics["zoom_400_viewport_width"])
        zoom_400_scroll_width = int(metrics["zoom_400_scroll_width"])
        zoom_400_clipped_interactive_count = int(
            metrics["zoom_400_clipped_interactive_count"]
        )
        route_fulfill_count = int(metrics["route_fulfill_count"])
        first_value_ms = float(metrics["first_value_ms"])
        first_value_cold_ms = float(metrics["first_value_cold_ms"])
        first_value_samples = [
            float(value) for value in list(metrics["first_value_samples_ms"])
        ]
        first_value_initial_samples = [
            float(value)
            for value in list(metrics["first_value_initial_samples_ms"])
        ]
        first_value_sample_count = int(metrics["first_value_sample_count"])
    except (KeyError, TypeError, ValueError, OverflowError):
        status_code = 0
        body_text_length = -1
        visible_interactive_count = -1
        visible_image_count = -1
        terminal_visible_image_count = -1
        broken_visible_image_count = -1
        zoom_400_percent = 0
        zoom_400_viewport_width = 0
        zoom_400_scroll_width = 0
        zoom_400_clipped_interactive_count = -1
        route_fulfill_count = -1
        first_value_ms = 0.0
        first_value_cold_ms = -1.0
        first_value_samples = []
        first_value_initial_samples = []
        first_value_sample_count = 0
    expected_status = ERROR_EXPECTED_STATUS if route == ERROR_ROUTE else 200
    first_value_retry_used = metrics.get("first_value_retry_used")
    samples_are_finite_positive = (
        len(first_value_samples) == FIRST_VALUE_SAMPLE_COUNT
        and all(math.isfinite(value) and value > 0 for value in first_value_samples)
    )
    initial_samples_are_finite_positive = (
        len(first_value_initial_samples) == FIRST_VALUE_SAMPLE_COUNT
        and all(
            math.isfinite(value) and value > 0
            for value in first_value_initial_samples
        )
    )
    first_value_median = (
        float(statistics.median(first_value_samples))
        if samples_are_finite_positive
        else 0.0
    )
    initial_first_value_median = (
        float(statistics.median(first_value_initial_samples))
        if initial_samples_are_finite_positive
        else 0.0
    )
    retry_coherent = isinstance(first_value_retry_used, bool) and (
        (
            first_value_retry_used is False
            and first_value_initial_samples == first_value_samples
        )
        or (
            first_value_retry_used is True
            and initial_first_value_median > float(first_value_budget_ms)
            and 0 < first_value_median <= float(first_value_budget_ms)
        )
    )
    first_value_contract_ok = (
        metrics.get("first_value_gated") is True
        and first_value_sample_count == FIRST_VALUE_SAMPLE_COUNT
        and samples_are_finite_positive
        and initial_samples_are_finite_positive
        and math.isfinite(first_value_cold_ms)
        and first_value_cold_ms >= 0
        and retry_coherent
        and math.isfinite(first_value_ms)
        and abs(first_value_median - first_value_ms) <= 0.5
        and metrics.get("first_value_basis") == FIRST_VALUE_BASIS
        and 0 < first_value_ms <= float(first_value_budget_ms)
    )
    checks = [
        {
            "name": "route_document_loaded",
            "ok": status_code == expected_status
            and str(metrics.get("final_route") or "") == route
            and str(metrics.get("document_ready_state") or "") in {"interactive", "complete"}
            and row.get("error") == ""
            and row.get("error_type") == ""
            and row.get("error_stage") == ""
            and row.get("error_detail") == "",
        },
        {
            "name": "structural_visual_contract",
            "ok": body_text_length > 0
            and metrics.get("navigation_visible") is True
            and visible_interactive_count > 0
            and metrics.get("horizontal_overflow") is False
            and visible_image_count >= 0
            and terminal_visible_image_count == visible_image_count
            and broken_visible_image_count == 0
            and (
                metrics.get("main_visible") is True
                or route == ERROR_ROUTE
            ),
        },
        {
            "name": "zoom_400_reflow",
            "ok": zoom_400_percent == 400
            and zoom_400_viewport_width == 320
            and 0 < zoom_400_scroll_width <= zoom_400_viewport_width + 2
            and metrics.get("zoom_400_reflow_without_horizontal_scroll") is True
            and zoom_400_clipped_interactive_count == 0,
        },
        {
            "name": "first_value_under_budget",
            "ok": browser_engine != FIRST_VALUE_ENGINE
            or first_value_contract_ok,
            "applicable": browser_engine == FIRST_VALUE_ENGINE,
            "observed_ms": first_value_ms,
            "budget_ms": float(first_value_budget_ms),
            "basis": str(metrics.get("first_value_basis") or ""),
        },
        {
            "name": "provider_response_not_mocked",
            "ok": metrics.get("provider_response_mocked") is False
            and metrics.get("request_interception_mode")
            == "origin_scoped_headers_continue_only"
            and route_fulfill_count == 0,
        },
    ]
    if route == ERROR_ROUTE:
        checks.extend(
            (
                {"name": "error_state_visible", "ok": metrics.get("error_state_visible") is True},
                {"name": "error_state_semantic", "ok": metrics.get("error_state_semantic") is True},
                {
                    "name": "error_state_recovers_online",
                    "ok": metrics.get("error_state_recovered_online") is True,
                },
            )
        )
    if route == SEARCH_ROUTE:
        checks.extend(
            (
                {"name": "loading_action_available", "ok": metrics.get("loading_action_available") is True},
                {"name": "loading_state_visible", "ok": metrics.get("loading_state_visible") is True},
                {"name": "loading_state_semantic", "ok": metrics.get("loading_state_semantic") is True},
            )
        )
    return checks


def build_continuous_ux_receipt(
    *,
    base_url: str,
    release_commit_sha: str,
    api_token: str,
    principal_id: str = "pq-continuous-ux-gate",
    storage_backend: str = "memory",
    routes: tuple[str, ...] = DEFAULT_ROUTES,
    browser_engines: tuple[str, ...] = SUPPORTED_PLAYWRIGHT_ENGINES,
    timeout_ms: int = 30_000,
    first_value_budget_ms: float = FIRST_VALUE_BUDGET_MS,
    visual_baseline_receipt: dict[str, Any] | None = None,
    visual_baseline_receipt_sha256: str = "",
    collect_engine_rows: Callable[..., list[dict[str, Any]]] = collect_continuous_ux_engine_rows,
) -> dict[str, Any]:
    engines = normalize_browser_engines(browser_engines)
    origin_error = loopback_origin_error(base_url)
    release_sha = str(release_commit_sha or "").strip().lower()
    candidate_bound = re.fullmatch(r"[0-9a-f]{40}", release_sha) is not None
    visual_baseline = dict(visual_baseline_receipt or {})
    visual_baseline_ok, visual_baseline_errors = validate_visual_baseline_receipt(
        visual_baseline,
        expected_release_commit_sha=release_sha,
    )
    visual_receipt_sha = str(visual_baseline_receipt_sha256 or "").strip().lower()
    try:
        embedded_visual_receipt_sha = visual_baseline_payload_sha256(
            visual_baseline
        )
    except (TypeError, ValueError):
        embedded_visual_receipt_sha = ""
    visual_receipt_sha_valid = bool(
        re.fullmatch(r"[0-9a-f]{64}", visual_receipt_sha)
        and visual_receipt_sha == embedded_visual_receipt_sha
    )
    visual_binding_errors = list(visual_baseline_errors)
    if not visual_receipt_sha_valid:
        visual_binding_errors.append("receipt_sha256_mismatch")
    memory_backend = str(storage_backend or "").strip().lower() == "memory"
    proof_mode = (
        REAL_PROOF_MODE
        if collect_engine_rows is collect_continuous_ux_engine_rows
        else MOCK_PROOF_MODE
    )
    headers = {
        "X-EA-Principal-ID": str(principal_id or "pq-continuous-ux-gate"),
        "Accept": "text/html,application/xhtml+xml",
    }
    if api_token:
        headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "X-EA-API-Token": api_token,
                "X-API-Token": api_token,
            }
        )
    rows: list[dict[str, Any]] = []
    engine_failures: list[dict[str, str]] = []
    prerequisites_ok = not origin_error and memory_backend and candidate_bound and bool(api_token)
    if prerequisites_ok:
        for engine in engines:
            try:
                rows.extend(
                    collect_engine_rows(
                        base_url=str(base_url or "").rstrip("/"),
                        browser_engine=engine,
                        routes=routes,
                        headers=headers,
                        timeout_ms=max(1_000, int(timeout_ms)),
                        first_value_budget_ms=float(first_value_budget_ms),
                    )
                )
            except Exception as exc:
                engine_failures.append(
                    {
                        "browser_engine": engine,
                        "error": "browser_engine_failed",
                        "error_type": type(exc).__name__,
                        "error_detail": _redacted_browser_error_detail(
                            exc,
                            sensitive_values=list(headers.values()),
                        ),
                    }
                )
    expected_samples = {(engine, route) for engine in engines for route in routes}
    row_sample_keys = [
        (str(row.get("browser_engine") or ""), str(row.get("route") or ""))
        for row in rows
    ]
    observed_samples = set(row_sample_keys)
    passed_samples = {
        (str(row.get("browser_engine") or ""), str(row.get("route") or ""))
        for row in rows
        if row.get("ok") is True
    }
    duplicate_samples = sorted(
        sample for sample in observed_samples if row_sample_keys.count(sample) > 1
    )
    missing_samples = sorted(expected_samples - observed_samples)
    row_checks = {
        (
            str(row.get("browser_engine") or ""),
            str(row.get("route") or ""),
            str(check.get("name") or ""),
        ): check.get("ok") is True
        for row in rows
        for check in list(row.get("checks") or [])
        if isinstance(check, dict)
    }
    loading_missing = [
        engine
        for engine in engines
        if not all(
            row_checks.get((engine, SEARCH_ROUTE, name)) is True
            for name in ("loading_action_available", "loading_state_visible", "loading_state_semantic", "provider_response_not_mocked")
        )
    ]
    error_missing = [
        engine
        for engine in engines
        if not all(
            row_checks.get((engine, ERROR_ROUTE, name)) is True
            for name in (
                "error_state_visible",
                "error_state_semantic",
                "error_state_recovers_online",
            )
        )
    ]
    failed_rows = [row for row in rows if row.get("ok") is not True]

    def route_fulfill_count(row: dict[str, Any]) -> int:
        try:
            return int(dict(row.get("metrics") or {})["route_fulfill_count"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return -1

    provider_response_mocking: bool | None
    if any(
        dict(row.get("metrics") or {}).get("provider_response_mocked") is True
        or route_fulfill_count(row) > 0
        for row in rows
    ):
        provider_response_mocking = True
    elif rows and all(
        dict(row.get("metrics") or {}).get("provider_response_mocked") is False
        and dict(row.get("metrics") or {}).get("request_interception_mode")
        == "origin_scoped_headers_continue_only"
        and route_fulfill_count(row) == 0
        for row in rows
    ):
        provider_response_mocking = False
    else:
        provider_response_mocking = None
    checks = [
        {"name": "isolated_loopback_origin", "ok": not origin_error, "reason": origin_error},
        {"name": "memory_storage_backend", "ok": memory_backend},
        {"name": "candidate_sha_bound", "ok": candidate_bound},
        {"name": "api_token_present_but_not_persisted", "ok": bool(api_token)},
        {"name": "production_claim_false", "ok": True},
        {"name": "real_playwright_browser_evidence", "ok": proof_mode == REAL_PROOF_MODE},
        {
            "name": "browser_engine_route_matrix_complete",
            "ok": not missing_samples
            and not duplicate_samples
            and not failed_rows
            and not engine_failures,
            "missing_samples": [
                {"browser_engine": engine, "route": route}
                for engine, route in missing_samples
            ],
            "engine_failures": engine_failures,
            "duplicate_samples": [
                {"browser_engine": engine, "route": route}
                for engine, route in duplicate_samples
            ],
        },
        {
            "name": "loading_error_state_matrix_complete",
            "ok": not loading_missing and not error_missing,
            "missing_loading_engines": loading_missing,
            "missing_error_engines": error_missing,
        },
        {
            "name": "structural_visual_matrix_complete",
            "ok": bool(rows)
            and all(
                row_checks.get((engine, route, "structural_visual_contract")) is True
                for engine, route in expected_samples
            ),
        },
        {
            "name": "zoom_400_matrix_complete",
            "ok": bool(rows)
            and all(
                row_checks.get((engine, route, "zoom_400_reflow")) is True
                for engine, route in expected_samples
            ),
        },
        {
            "name": "first_value_budget_matrix_complete",
            "ok": bool(rows)
            and FIRST_VALUE_ENGINE in engines
            and all(
                row_checks.get((engine, route, "first_value_under_budget")) is True
                for engine, route in expected_samples
                if engine == FIRST_VALUE_ENGINE
            ),
            "budget_ms": float(first_value_budget_ms),
            "browser_engine": FIRST_VALUE_ENGINE,
            "sample_count": FIRST_VALUE_SAMPLE_COUNT,
            "max_attempts": FIRST_VALUE_MAX_ATTEMPTS,
        },
        {
            "name": "provider_response_mocking_forbidden",
            "ok": proof_mode == REAL_PROOF_MODE
            and provider_response_mocking is False,
        },
        {
            "name": "screenshot_pixel_comparison_complete",
            "ok": visual_baseline_ok and visual_receipt_sha_valid,
            "receipt_sha256": visual_receipt_sha,
            "errors": visual_binding_errors,
        },
    ]
    failed_checks = [check for check in checks if check.get("ok") is not True]
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": (
            "pass"
            if not missing_samples
            and not duplicate_samples
            and not failed_rows
            and not engine_failures
            and not failed_checks
            else ("blocked" if not prerequisites_ok else "fail")
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_commit_sha": release_sha,
        "proof_scope": PROOF_SCOPE,
        "proof_mode": proof_mode,
        "production_claim": False,
        "deployed_or_live_proof": False,
        "storage_backend": "memory" if memory_backend else str(storage_backend or ""),
        "base_origin_kind": "loopback" if not origin_error else "invalid",
        "provider_response_mocking": provider_response_mocking,
        "screenshot_pixel_comparison": visual_baseline.get(
            "screenshot_pixel_comparison"
        ) is True,
        "visual_baseline_receipt_sha256": visual_receipt_sha,
        "visual_baseline": visual_baseline,
        "first_value_budget_ms": float(first_value_budget_ms),
        "first_value_basis": FIRST_VALUE_BASIS,
        "first_value_max_attempts": FIRST_VALUE_MAX_ATTEMPTS,
        "required_browser_engines": list(engines),
        "required_routes": list(routes),
        "required_state_kinds": list(REQUIRED_STATE_KINDS),
        "expected_sample_count": len(expected_samples),
        "observed_sample_count": len(observed_samples),
        "passed_sample_count": len(passed_samples),
        "missing_sample_count": len(missing_samples),
        "duplicate_sample_count": len(duplicate_samples),
        "failed_count": len(failed_rows) + len(engine_failures),
        "checks": checks,
        "rows": rows,
        "engine_failures": engine_failures,
        "notes": [
            "This receipt proves an isolated loopback in-memory browser gate only and cannot establish deployed or production readiness.",
            "Visual regression combines stable DOM, overflow, image, clipping, and reflow invariants with a candidate-bound Chromium screenshot pixel comparison.",
            "First value gates the median of three warm Chromium DOMContentLoaded samples after observing visible structure, with one bounded retry for transient runner contention; cold and initial samples remain diagnostic, other engines are diagnostic, and first-contentful-paint is retained as diagnostic evidence.",
            "The loading interaction uses the real search launch control; the route handler only continues requests with origin-scoped headers and never fulfills or mocks provider responses.",
            "The error interaction uses the browser's real offline transition and the product's semantic offline marker without mocking a provider response.",
            "Each route runs in a fresh browser context so cookies, pending requests, and emulated network state cannot bleed between observations.",
        ],
    }
    serialized = json.dumps(receipt, sort_keys=True)
    if api_token and api_token in serialized:
        raise RuntimeError("continuous_ux_receipt_secret_leak")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the supplemental isolated-loopback PropertyQuarry continuous UX gate."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PROPERTYQUARRY_CONTINUOUS_UX_BASE_URL", "http://127.0.0.1:8097"),
    )
    parser.add_argument(
        "--release-sha",
        default=os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA", ""),
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("PROPERTYQUARRY_CONTINUOUS_UX_API_TOKEN")
        or os.environ.get("EA_API_TOKEN", ""),
    )
    parser.add_argument(
        "--principal-id",
        default=os.environ.get("PROPERTYQUARRY_CONTINUOUS_UX_PRINCIPAL_ID", "pq-continuous-ux-gate"),
    )
    parser.add_argument(
        "--storage-backend",
        default=os.environ.get("EA_STORAGE_BACKEND", ""),
    )
    parser.add_argument(
        "--browser-engines",
        default=",".join(SUPPORTED_PLAYWRIGHT_ENGINES),
    )
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--first-value-budget-ms", type=float, default=FIRST_VALUE_BUDGET_MS)
    parser.add_argument(
        "--visual-baseline-receipt",
        default=os.environ.get("PROPERTYQUARRY_VISUAL_BASELINE_RECEIPT", ""),
    )
    parser.add_argument(
        "--write",
        default="_completion/smoke/propertyquarry-continuous-ux-latest.json",
    )
    args = parser.parse_args()
    try:
        engines = normalize_browser_engines(
            tuple(
                value.strip()
                for value in str(args.browser_engines or "").split(",")
                if value.strip()
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    visual_baseline_receipt: dict[str, Any] = {}
    visual_baseline_receipt_sha256 = ""
    if str(args.visual_baseline_receipt or "").strip():
        try:
            (
                visual_baseline_receipt,
                visual_baseline_receipt_sha256,
            ) = load_visual_baseline_receipt(
                Path(str(args.visual_baseline_receipt).strip())
            )
        except ValueError as exc:
            parser.error(str(exc))
    receipt = build_continuous_ux_receipt(
        base_url=str(args.base_url or "").strip(),
        release_commit_sha=str(args.release_sha or "").strip(),
        api_token=str(args.api_token or "").strip(),
        principal_id=str(args.principal_id or "").strip() or "pq-continuous-ux-gate",
        storage_backend=str(args.storage_backend or "").strip(),
        browser_engines=engines,
        timeout_ms=max(1_000, int(args.timeout_ms)),
        first_value_budget_ms=max(1.0, float(args.first_value_budget_ms)),
        visual_baseline_receipt=visual_baseline_receipt,
        visual_baseline_receipt_sha256=visual_baseline_receipt_sha256,
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        output_path = Path(args.write)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
