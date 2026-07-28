from __future__ import annotations

import base64
import json
import time
from io import BytesIO
from pathlib import Path

import pytest

from scripts import propertyquarry_continuous_ux_gate as gate


ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


def _passing_row(*, engine: str, route: str) -> dict[str, object]:
    first_value_samples = (
        [210.0, 220.0, 230.0]
        if engine == gate.FIRST_VALUE_ENGINE
        else [220.0]
    )
    metrics: dict[str, object] = {
        "document_ready_state": "complete",
        "final_route": route,
        "main_visible": route != gate.ERROR_ROUTE,
        "navigation_visible": True,
        "body_text_length": 120,
        "visible_interactive_count": 4,
        "horizontal_overflow": False,
        "visible_image_count": 0,
        "terminal_visible_image_count": 0,
        "broken_visible_image_count": 0,
        "zoom_400_percent": 400,
        "zoom_400_viewport_width": 320,
        "zoom_400_scroll_width": 320,
        "zoom_400_reflow_without_horizontal_scroll": True,
        "zoom_400_clipped_interactive_count": 0,
        "first_value_ms": 220.0,
        "first_value_cold_ms": 260.0,
        "first_value_initial_samples_ms": first_value_samples,
        "first_value_samples_ms": first_value_samples,
        "first_value_sample_count": len(first_value_samples),
        "first_value_retry_used": False,
        "first_value_gated": engine == gate.FIRST_VALUE_ENGINE,
        "first_value_basis": gate.FIRST_VALUE_BASIS,
        "provider_response_mocked": False,
        "request_interception_mode": "origin_scoped_headers_continue_only",
        "route_fulfill_count": 0,
    }
    if route == gate.SEARCH_ROUTE:
        metrics.update(
            {
                "loading_action_available": True,
                "loading_state_visible": True,
                "loading_state_semantic": True,
            }
        )
    if route == gate.ERROR_ROUTE:
        metrics.update(
            {
                "error_state_visible": True,
                "error_state_semantic": True,
                "error_state_recovered_online": True,
            }
        )
    row: dict[str, object] = {
        "route": route,
        "browser_engine": engine,
        "status_code": gate.ERROR_EXPECTED_STATUS if route == gate.ERROR_ROUTE else 200,
        "metrics": metrics,
        "error": "",
        "error_type": "",
        "error_stage": "",
        "error_detail": "",
    }
    checks = gate.evaluate_continuous_ux_row(row)
    row["checks"] = checks
    row["ok"] = all(check["ok"] is True for check in checks)
    return row


def _passing_visual_baseline_receipt() -> dict[str, object]:
    release_sha = "a" * 40
    case_ids = list(gate.VISUAL_BASELINE_REQUIRED_CASE_IDS)
    capture = dict(gate.VISUAL_BASELINE_CAPTURE_CONTRACT)
    browser_version = "Chromium 140.0.7339.16"
    playwright_version = "1.54.0"
    expected_actual_pngs = sorted(f"{case_id}.png" for case_id in case_ids)
    source_binding = {
        "schema": gate.SOURCE_BINDING_SCHEMA,
        "generated_at": "2026-07-13T09:59:59+00:00",
        "status": "pass",
        "required_checks": list(gate.SOURCE_BINDING_REQUIRED_CHECKS),
        "failure_count": 0,
        "failures": [],
        "manifest_runtime_commit": release_sha,
        "head_commit": release_sha,
        "parent_commit": "2" * 40,
        "merge_commit_required": False,
        "merge_base_parent_commit": "",
        "merge_parent_commits": ["2" * 40],
        "head_tree": "3" * 40,
        "reviewed_envelope_commit": "",
        "reviewed_envelope_parent_commits": [],
        "reviewed_envelope_tree": "",
        "merge_tree_matches_reviewed_envelope": False,
        "manifest_descendant_paths": [],
        "manifest_metadata_only_ancestor": False,
        "tracked_dirty_path_count": 0,
        "untracked_release_source_count": 0,
        "note": "Repository hygiene and release-manifest authority gate.",
    }
    return {
        "schema": gate.VISUAL_BASELINE_SCHEMA,
        "generated_at": "2026-07-13T10:00:00+00:00",
        "status": "pass",
        "release_commit_sha": release_sha,
        "expected_release_commit_sha": release_sha,
        "proof_mode": gate.VISUAL_BASELINE_PROOF_MODE,
        "screenshot_pixel_comparison": True,
        "update_mode": False,
        "receipt_written": True,
        "source_binding_receipt_sha256": gate.source_binding_payload_sha256(
            source_binding
        ),
        "source_binding": source_binding,
        "manifest": {
            "schema": gate.VISUAL_BASELINE_MANIFEST_SCHEMA,
            "sha256": "b" * 64,
            "git_blob_sha1": "c" * 40,
            "case_count": len(case_ids),
            "error": "",
        },
        "browser": {
            "name": "chromium",
            "version": browser_version,
            "playwright_version": playwright_version,
            "fingerprint_sha256": gate.visual_baseline_payload_sha256(
                {
                    "browser_engine": "chromium",
                    "browser_version": browser_version,
                    "playwright_version": playwright_version,
                    "capture": capture,
                }
            ),
            "capture": capture,
        },
        "comparison": {
            "algorithm": gate.VISUAL_BASELINE_ALGORITHM,
            "pixel_threshold": 0.1,
            "max_changed_pixel_ratio": 0.005,
        },
        "expected_case_ids": case_ids,
        "observed_case_ids": case_ids,
        "preflight": {
            "errors": [],
            "expected_actual_pngs": expected_actual_pngs,
            "observed_actual_pngs": expected_actual_pngs,
            "missing_actual_pngs": [],
            "extra_actual_pngs": [],
            "path_graph_safe": True,
            "actual_workspace_safe": True,
            "diff_workspace_safe": True,
        },
        "outcome_count": len(case_ids),
        "failed_count": 0,
        "checks": [
            {"name": name, "ok": True}
            for name in gate.VISUAL_BASELINE_REQUIRED_CHECKS
        ],
        "outcomes": [
            {
                "case_id": case_id,
                "status": "pass",
                "reasons": [],
                "baseline_path": f"images/{case_id}.png",
                "actual_path": f"{case_id}.png",
                "diff_path": f"{case_id}.diff.png",
                "expected_dimensions": {"width": width, "height": height},
                "baseline_dimensions": {"width": width, "height": height},
                "actual_dimensions": {"width": width, "height": height},
                "baseline_sha256": "e" * 64,
                "expected_baseline_sha256": "e" * 64,
                "actual_sha256": "f" * 64,
                "diff_sha256": "1" * 64,
                "changed_pixel_count": 0,
                "total_pixel_count": width * height,
                "changed_pixel_ratio": 0.0,
                "maximum_yiq_delta": 0.0,
            }
            for case_id, width, height in gate.VISUAL_BASELINE_REQUIRED_CASES
        ],
    }


def test_visual_baseline_receipt_validation_is_candidate_bound_and_fail_closed() -> None:
    receipt = _passing_visual_baseline_receipt()

    ok, errors = gate.validate_visual_baseline_receipt(
        receipt,
        expected_release_commit_sha="a" * 40,
    )

    assert ok is True
    assert errors == []

    tampered = dict(receipt)
    tampered["observed_case_ids"] = list(receipt["observed_case_ids"])[1:]
    ok, errors = gate.validate_visual_baseline_receipt(
        tampered,
        expected_release_commit_sha="a" * 40,
    )
    assert ok is False
    assert "observed_case_matrix_mismatch" in errors

    wrong_candidate = dict(receipt)
    wrong_candidate["release_commit_sha"] = "b" * 40
    ok, errors = gate.validate_visual_baseline_receipt(
        wrong_candidate,
        expected_release_commit_sha="a" * 40,
    )
    assert ok is False
    assert "release_commit_sha_mismatch" in errors

    coerced_capture = _passing_visual_baseline_receipt()
    coerced_capture["browser"]["capture"]["device_scale_factor"] = True
    ok, errors = gate.validate_visual_baseline_receipt(
        coerced_capture,
        expected_release_commit_sha="a" * 40,
    )
    assert ok is False
    assert "capture_contract_types_invalid" in errors

    extra_field = _passing_visual_baseline_receipt()
    extra_field["unbound_claim"] = True
    ok, errors = gate.validate_visual_baseline_receipt(
        extra_field,
        expected_release_commit_sha="a" * 40,
    )
    assert ok is False
    assert "receipt_keys_invalid" in errors

    coerced_ratio = _passing_visual_baseline_receipt()
    coerced_ratio["outcomes"][0]["changed_pixel_ratio"] = "0.0"
    ok, errors = gate.validate_visual_baseline_receipt(
        coerced_ratio,
        expected_release_commit_sha="a" * 40,
    )
    assert ok is False
    assert "changed_pixel_ratio_invalid" in errors


def test_visual_baseline_receipt_loader_rejects_aliases_and_noncanonical_json(
    tmp_path: Path,
) -> None:
    receipt = _passing_visual_baseline_receipt()
    receipt_path = tmp_path / "visual-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    loaded, digest = gate.load_visual_baseline_receipt(receipt_path)
    assert loaded == receipt
    assert digest == gate.visual_baseline_payload_sha256(receipt)

    alias_path = tmp_path / "visual-receipt-alias.json"
    alias_path.symlink_to(receipt_path)
    with pytest.raises(ValueError, match="visual_baseline_receipt_regular_file_required"):
        gate.load_visual_baseline_receipt(alias_path)

    duplicate_path = tmp_path / "visual-receipt-duplicate.json"
    duplicate_path.write_text(
        json.dumps(receipt).replace(
            '"status": "pass"',
            '"status": "pass", "status": "pass"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="visual_baseline_receipt_json_invalid"):
        gate.load_visual_baseline_receipt(duplicate_path)

    nonfinite_path = tmp_path / "visual-receipt-nonfinite.json"
    nonfinite_path.write_text(
        json.dumps(receipt).replace('"failed_count": 0', '"failed_count": NaN', 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="visual_baseline_receipt_json_invalid"):
        gate.load_visual_baseline_receipt(nonfinite_path)


def test_continuous_ux_gate_rejects_non_loopback_or_non_memory_before_browser() -> None:
    called = False

    def fake_collect(**_kwargs):
        nonlocal called
        called = True
        return []

    receipt = gate.build_continuous_ux_receipt(
        base_url="https://propertyquarry.com",
        release_commit_sha="a" * 40,
        api_token="ephemeral-token",
        storage_backend="postgres",
        collect_engine_rows=fake_collect,
    )

    assert receipt["status"] == "blocked"
    assert called is False
    assert receipt["production_claim"] is False
    assert receipt["deployed_or_live_proof"] is False
    assert receipt["base_origin_kind"] == "invalid"
    assert receipt["proof_mode"] == gate.MOCK_PROOF_MODE
    assert "ephemeral-token" not in str(receipt)


def test_continuous_ux_gate_contract_collector_cannot_claim_real_browser_pass() -> None:
    def fake_collect(**kwargs):
        return [
            _passing_row(engine=kwargs["browser_engine"], route=route)
            for route in kwargs["routes"]
        ]

    receipt = gate.build_continuous_ux_receipt(
        base_url="http://127.0.0.1:8097",
        release_commit_sha="a" * 40,
        api_token="ephemeral-token",
        storage_backend="memory",
        browser_engines=("chromium", "firefox", "webkit"),
        collect_engine_rows=fake_collect,
    )

    assert receipt["status"] == "fail"
    assert receipt["proof_mode"] == gate.MOCK_PROOF_MODE
    real_check = next(
        check
        for check in receipt["checks"]
        if check["name"] == "real_playwright_browser_evidence"
    )
    assert real_check["ok"] is False
    assert receipt["expected_sample_count"] == len(gate.DEFAULT_ROUTES) * 3
    assert receipt["observed_sample_count"] == len(gate.DEFAULT_ROUTES) * 3
    assert receipt["provider_response_mocking"] is False
    assert receipt["screenshot_pixel_comparison"] is False


def test_continuous_ux_gate_binds_embedded_visual_receipt_digest() -> None:
    def fake_collect(**kwargs):
        return [
            _passing_row(engine=kwargs["browser_engine"], route=route)
            for route in kwargs["routes"]
        ]

    visual = _passing_visual_baseline_receipt()
    visual_sha = gate.visual_baseline_payload_sha256(visual)
    receipt = gate.build_continuous_ux_receipt(
        base_url="http://127.0.0.1:8097",
        release_commit_sha="a" * 40,
        api_token="ephemeral-token",
        storage_backend="memory",
        browser_engines=("chromium", "firefox", "webkit"),
        visual_baseline_receipt=visual,
        visual_baseline_receipt_sha256=visual_sha,
        collect_engine_rows=fake_collect,
    )
    visual_check = next(
        check
        for check in receipt["checks"]
        if check["name"] == "screenshot_pixel_comparison_complete"
    )
    assert visual_check["ok"] is True
    assert visual_check["errors"] == []
    assert receipt["visual_baseline_receipt_sha256"] == visual_sha

    tampered_binding = gate.build_continuous_ux_receipt(
        base_url="http://127.0.0.1:8097",
        release_commit_sha="a" * 40,
        api_token="ephemeral-token",
        storage_backend="memory",
        browser_engines=("chromium", "firefox", "webkit"),
        visual_baseline_receipt=visual,
        visual_baseline_receipt_sha256="9" * 64,
        collect_engine_rows=fake_collect,
    )
    visual_check = next(
        check
        for check in tampered_binding["checks"]
        if check["name"] == "screenshot_pixel_comparison_complete"
    )
    assert visual_check["ok"] is False
    assert "receipt_sha256_mismatch" in visual_check["errors"]


def test_continuous_ux_row_fails_closed_on_visual_zoom_state_or_budget_regression() -> None:
    search = _passing_row(engine="chromium", route=gate.SEARCH_ROUTE)
    metrics = dict(search["metrics"])
    metrics.update(
        {
            "broken_visible_image_count": 1,
            "zoom_400_clipped_interactive_count": 1,
            "first_value_ms": gate.FIRST_VALUE_BUDGET_MS + 1.0,
            "first_value_basis": "untrusted_stopwatch",
            "loading_state_semantic": False,
            "provider_response_mocked": True,
        }
    )
    search["metrics"] = metrics

    failed = {
        check["name"]
        for check in gate.evaluate_continuous_ux_row(search)
        if check["ok"] is not True
    }

    assert failed == {
        "structural_visual_contract",
        "zoom_400_reflow",
        "first_value_under_budget",
        "loading_state_semantic",
        "provider_response_not_mocked",
    }


def test_visible_image_wait_stabilizes_declared_sources_before_decode() -> None:
    class FakePage:
        script = ""
        argument: dict[str, int] = {}

        def evaluate(self, script: str, argument: dict[str, int]) -> None:
            self.script = script
            self.argument = argument

    page = FakePage()
    gate._wait_for_visible_image_terminal_state(page, timeout_ms=30_000)

    assert page.argument == {
        "timeoutMs": 10_000,
        "stabilityMs": gate.VISIBLE_IMAGE_STABILITY_MS,
    }
    assert "image.currentSrc" in page.script
    assert "image.getAttribute('src')" in page.script
    assert "image.getAttribute('srcset')" in page.script
    assert "requestAnimationFrame" in page.script
    assert "Promise.allSettled" in page.script
    assert "image.naturalWidth" not in page.script
    assert "visible_image_terminal_state_timeout" in page.script


def test_browser_error_detail_is_bounded_and_redacts_sensitive_values() -> None:
    secret = "local-super-secret-token"
    detail = gate._redacted_browser_error_detail(
        RuntimeError(f"first line\nBearer {secret} second line {secret}"),
        sensitive_values=[f"Bearer {secret}", secret],
        limit=48,
    )

    assert secret not in detail
    assert "\n" not in detail
    assert "[redacted]" in detail
    assert len(detail) <= 48


def test_visible_image_wait_handles_hydration_breakage_and_frame_timeout() -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(20, 120, 80)).save(buffer, format="PNG")
    valid_source = "data:image/png;base64," + base64.b64encode(
        buffer.getvalue()
    ).decode("ascii")
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        except Exception as exc:  # pragma: no cover - developer machines may omit browsers
            pytest.skip(f"chromium unavailable for continuous UX contract: {type(exc).__name__}")
        try:
            page = browser.new_page()
            page.set_content("<main>hydrating</main><nav aria-label='Primary'>nav</nav>")
            page.evaluate(
                """
                ([source]) => setTimeout(() => {
                  const image = document.createElement('img');
                  image.alt = 'delayed';
                  image.style.cssText = 'width:20px;height:20px';
                  image.src = source;
                  document.body.appendChild(image);
                }, 75)
                """,
                [valid_source],
            )
            gate._wait_for_visible_image_terminal_state(page, timeout_ms=2_000)
            delayed = gate._structural_visual_metrics(page)
            assert delayed["visible_image_count"] == 1
            assert delayed["terminal_visible_image_count"] == 1
            assert delayed["broken_visible_image_count"] == 0

            page.set_content(
                "<main>broken</main><nav aria-label='Primary'>nav</nav>"
                "<img alt='broken' style='width:20px;height:20px' "
                "src='data:image/png;base64,broken'>"
            )
            gate._wait_for_visible_image_terminal_state(page, timeout_ms=2_000)
            broken = gate._structural_visual_metrics(page)
            assert broken["visible_image_count"] == 1
            assert broken["terminal_visible_image_count"] == 1
            assert broken["broken_visible_image_count"] == 1

            page.set_content(
                "<main>decode stall</main><nav aria-label='Primary'>nav</nav>"
                f"<img id='decode-stall' alt='valid' style='width:20px;height:20px' "
                f"src='{valid_source}'>"
            )
            page.locator("#decode-stall").wait_for(state="visible")
            page.wait_for_function(
                "document.querySelector('#decode-stall').naturalWidth > 0"
            )
            page.evaluate(
                "document.querySelector('#decode-stall').decode = () => "
                "new Promise((resolve) => setTimeout(resolve, 2000))"
            )
            gate._wait_for_visible_image_terminal_state(page, timeout_ms=1_500)
            stalled_decode = gate._structural_visual_metrics(page)
            assert stalled_decode["visible_image_count"] == 1
            assert stalled_decode["terminal_visible_image_count"] == 1
            assert stalled_decode["broken_visible_image_count"] == 0

            page.set_content(
                "<main>missing</main><nav aria-label='Primary'>nav</nav>"
                "<img alt='missing' style='width:20px;height:20px' src=''>"
            )
            with pytest.raises(Exception, match="visible_image_terminal_state_timeout"):
                gate._wait_for_visible_image_terminal_state(page, timeout_ms=1_000)

            page.set_content("<main>no frames</main><nav aria-label='Primary'>nav</nav>")
            page.evaluate("window.requestAnimationFrame = () => 0")
            started = time.monotonic()
            with pytest.raises(Exception, match="visible_image_terminal_state_timeout"):
                gate._wait_for_visible_image_terminal_state(page, timeout_ms=1_000)
            assert time.monotonic() - started < 2.5
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("route", "target", "field", "value"),
    (
        (gate.SEARCH_ROUTE, "metrics", "loading_action_available", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "loading_state_visible", False),
        (gate.SEARCH_ROUTE, "metrics", "loading_state_semantic", _MISSING),
        (gate.ERROR_ROUTE, "metrics", "error_state_visible", _MISSING),
        (gate.ERROR_ROUTE, "metrics", "error_state_semantic", False),
        (gate.ERROR_ROUTE, "metrics", "error_state_recovered_online", _MISSING),
        (gate.SEARCH_ROUTE, "row", "status_code", _MISSING),
        (gate.SEARCH_ROUTE, "row", "status_code", 503),
        (gate.SEARCH_ROUTE, "metrics", "document_ready_state", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "document_ready_state", "loading"),
        (gate.SEARCH_ROUTE, "row", "error", _MISSING),
        (gate.SEARCH_ROUTE, "row", "error", "playwright_timeout"),
        (gate.SEARCH_ROUTE, "row", "error_type", _MISSING),
        (gate.SEARCH_ROUTE, "row", "error_stage", "route_navigation"),
        (gate.SEARCH_ROUTE, "row", "error_detail", "net::ERR_ABORTED"),
        (gate.SEARCH_ROUTE, "metrics", "first_value_retry_used", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "first_value_retry_used", True),
        (gate.SEARCH_ROUTE, "metrics", "first_value_cold_ms", -1.0),
        (gate.SEARCH_ROUTE, "metrics", "first_value_cold_ms", float("inf")),
        (gate.SEARCH_ROUTE, "metrics", "first_value_initial_samples_ms", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "first_value_initial_samples_ms", [210.0, 220.0]),
        (gate.SEARCH_ROUTE, "metrics", "zoom_400_viewport_width", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "zoom_400_viewport_width", 321),
        (gate.SEARCH_ROUTE, "metrics", "zoom_400_scroll_width", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "horizontal_overflow", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "horizontal_overflow", True),
        (gate.SEARCH_ROUTE, "metrics", "provider_response_mocked", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "provider_response_mocked", True),
        (gate.SEARCH_ROUTE, "metrics", "request_interception_mode", _MISSING),
        (gate.SEARCH_ROUTE, "metrics", "route_fulfill_count", _MISSING),
    ),
)
def test_continuous_ux_row_rejects_missing_or_tampered_raw_evidence(
    route: str,
    target: str,
    field: str,
    value: object,
) -> None:
    row = _passing_row(engine=gate.FIRST_VALUE_ENGINE, route=route)
    evidence = row if target == "row" else row["metrics"]
    assert isinstance(evidence, dict)
    if value is _MISSING:
        evidence.pop(field)
    else:
        evidence[field] = value

    assert any(
        check["ok"] is not True
        for check in gate.evaluate_continuous_ux_row(row)
    )


def test_continuous_ux_gate_is_part_of_the_local_release_plane() -> None:
    release_gate = (ROOT / "scripts/property_release_gates.sh").read_text(
        encoding="utf-8"
    )
    gate_script = (ROOT / "scripts/propertyquarry_continuous_ux_gate.py").read_text(
        encoding="utf-8"
    )

    assert "PROPERTYQUARRY_CONTINUOUS_UX_RECEIPT" in release_gate
    assert "continuous_ux_receipt" in release_gate
    assert "PROPERTYQUARRY_RELEASE_COMMIT_SHA" in gate_script
    assert "GITHUB_SHA" not in gate_script
