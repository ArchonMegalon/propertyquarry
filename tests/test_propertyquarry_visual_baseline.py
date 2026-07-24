from __future__ import annotations

import hashlib
import json
import stat
import struct
import zlib
from pathlib import Path

import pytest

from scripts import propertyquarry_visual_baseline as visual


RELEASE_SHA = "a" * 40


def _source_binding_receipt(
    *,
    release_sha: str = RELEASE_SHA,
    workflow_head_sha: str = RELEASE_SHA,
    binding_parent_sha: str | None = None,
    merge_parent_shas: list[str] | None = None,
) -> dict[str, object]:
    same_commit = release_sha == workflow_head_sha
    parent_sha = (
        "b" * 40
        if same_commit
        else binding_parent_sha or release_sha
    )
    return {
        "schema": visual.SOURCE_BINDING_SCHEMA,
        "generated_at": "2026-07-16T22:00:00+00:00",
        "status": "pass",
        "required_checks": list(visual.SOURCE_BINDING_REQUIRED_CHECKS),
        "failure_count": 0,
        "failures": [],
        "manifest_runtime_commit": release_sha,
        "head_commit": workflow_head_sha,
        "parent_commit": parent_sha,
        "merge_commit_required": False,
        "merge_base_parent_commit": "",
        "merge_parent_commits": (
            list(merge_parent_shas)
            if merge_parent_shas is not None
            else [parent_sha]
        ),
        "head_tree": "e" * 40,
        "reviewed_envelope_commit": "",
        "reviewed_envelope_parent_commits": [],
        "reviewed_envelope_tree": "",
        "merge_tree_matches_reviewed_envelope": False,
        "manifest_descendant_paths": (
            [] if same_commit else list(visual.RELEASE_METADATA_DESCENDANT_PATHS)
        ),
        "manifest_metadata_only_ancestor": not same_commit,
        "tracked_dirty_path_count": 0,
        "untracked_release_source_count": 0,
        "note": "Repository hygiene and release-manifest authority gate for the tracked PropertyQuarry release plane.",
    }


def _rgba(width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
    return bytes(color) * (width * height)


def _png(
    width: int,
    height: int,
    color: tuple[int, int, int, int] = (120, 130, 140, 255),
) -> bytes:
    return visual.encode_rgba_png(width, height, _rgba(width, height, color))


def _manifest_payload(
    cases: list[dict[str, object]],
    *,
    pixel_threshold: float = 0.1,
    max_changed_pixel_ratio: float = 0.005,
) -> dict[str, object]:
    return {
        "schema": visual.MANIFEST_SCHEMA,
        "version": 1,
        "capture": dict(visual.CAPTURE_CONTRACT),
        "comparison": {
            "algorithm": visual.COMPARISON_ALGORITHM,
            "pixel_threshold": pixel_threshold,
            "max_changed_pixel_ratio": max_changed_pixel_ratio,
        },
        "cases": cases,
    }


def _write_case_matrix(
    root: Path,
    *,
    baseline_payloads: dict[str, bytes],
    actual_payloads: dict[str, bytes] | None = None,
    dimensions: dict[str, tuple[int, int]] | None = None,
    pixel_threshold: float = 0.1,
    max_changed_pixel_ratio: float = 0.005,
) -> tuple[Path, Path, Path, Path]:
    manifest_path = root / "baselines" / "manifest.json"
    actual_dir = root / "actuals"
    diff_dir = root / "diffs"
    receipt_path = root / "receipt.json"
    cases: list[dict[str, object]] = []
    for case_id, baseline_payload in baseline_payloads.items():
        decoded = visual.decode_png(baseline_payload)
        expected_width, expected_height = (dimensions or {}).get(
            case_id, (decoded.width, decoded.height)
        )
        baseline_relative = f"images/{case_id}.png"
        baseline_path = manifest_path.parent / baseline_relative
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_bytes(baseline_payload)
        actual_dir.mkdir(parents=True, exist_ok=True)
        actual_dir.joinpath(f"{case_id}.png").write_bytes(
            (actual_payloads or {}).get(case_id, baseline_payload)
        )
        cases.append(
            {
                "id": case_id,
                "baseline": baseline_relative,
                "width": expected_width,
                "height": expected_height,
                "sha256": hashlib.sha256(baseline_payload).hexdigest(),
            }
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            _manifest_payload(
                cases,
                pixel_threshold=pixel_threshold,
                max_changed_pixel_ratio=max_changed_pixel_ratio,
            )
        ),
        encoding="utf-8",
    )
    return manifest_path, actual_dir, diff_dir, receipt_path


def _verify(
    *,
    manifest_path: Path,
    actual_dir: Path,
    diff_dir: Path,
    receipt_path: Path,
    release_sha: str = RELEASE_SHA,
    expected_release_sha: str = RELEASE_SHA,
    workflow_head_sha: str = RELEASE_SHA,
    source_binding_receipt: dict[str, object] | None = None,
) -> tuple[dict[str, object], int]:
    source_binding = source_binding_receipt or _source_binding_receipt(
        release_sha=release_sha,
        workflow_head_sha=workflow_head_sha,
    )
    return visual.verify_visual_baselines(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        release_commit_sha=release_sha,
        expected_release_commit_sha=expected_release_sha,
        workflow_head_sha=workflow_head_sha,
        source_binding_receipt=source_binding,
        source_binding_receipt_sha256=visual.source_binding_payload_sha256(
            source_binding
        ),
        browser_version="Chromium 140.0.7339.16",
        playwright_version="1.54.0",
    )


def _filtered_png(
    *,
    width: int,
    rows: list[bytes],
    channels: int,
) -> tuple[bytes, bytes]:
    filtered_rows = bytearray()
    previous = bytes(width * channels)
    for filter_type, row in enumerate(rows):
        filtered_rows.append(filter_type)
        encoded = bytearray(len(row))
        for index, value in enumerate(row):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            else:
                predictor = visual._paeth(left, up, upper_left)
            encoded[index] = (value - predictor) & 0xFF
        filtered_rows.extend(encoded)
        previous = row
    color_type = 2 if channels == 3 else 6
    ihdr = struct.pack(">IIBBBBB", width, len(rows), 8, color_type, 0, 0, 0)
    payload = (
        visual.PNG_SIGNATURE
        + visual._png_chunk(b"IHDR", ihdr)
        + visual._png_chunk(b"IDAT", zlib.compress(bytes(filtered_rows)))
        + visual._png_chunk(b"IEND", b"")
    )
    expected_rgba = bytearray()
    for row in rows:
        for offset in range(0, len(row), channels):
            expected_rgba.extend(row[offset : offset + 3])
            expected_rgba.append(row[offset + 3] if channels == 4 else 255)
    return payload, bytes(expected_rgba)


@pytest.mark.parametrize("channels", (3, 4))
def test_png_decoder_supports_playwright_rgb_rgba_and_all_filters(channels: int) -> None:
    width = 4
    rows = [
        bytes(
            ((row_index * 37 + byte_index * 19 + channels) % 256)
            for byte_index in range(width * channels)
        )
        for row_index in range(5)
    ]
    payload, expected_rgba = _filtered_png(
        width=width,
        rows=rows,
        channels=channels,
    )

    decoded = visual.decode_png(payload)

    assert (decoded.width, decoded.height) == (width, 5)
    assert decoded.rgba == expected_rgba


def test_png_decoder_rejects_crc_corruption_and_oversized_dimensions() -> None:
    valid = bytearray(_png(3, 2))
    valid[-5] ^= 1
    with pytest.raises(visual.VisualBaselineError, match="png_chunk_crc_invalid"):
        visual.decode_png(bytes(valid))

    huge_ihdr = struct.pack(
        ">IIBBBBB", visual.MAX_DIMENSION + 1, 1, 8, 6, 0, 0, 0
    )
    huge = (
        visual.PNG_SIGNATURE
        + visual._png_chunk(b"IHDR", huge_ihdr)
        + visual._png_chunk(b"IDAT", zlib.compress(b"\x00"))
        + visual._png_chunk(b"IEND", b"")
    )
    with pytest.raises(visual.VisualBaselineError, match="png_dimensions_out_of_bounds"):
        visual.decode_png(huge)


def test_png_decoder_bounds_chunk_count_and_aggregate_idat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _png(3, 2)
    monkeypatch.setattr(visual, "MAX_PNG_CHUNKS", 2)
    with pytest.raises(visual.VisualBaselineError, match="png_chunk_count_exceeded"):
        visual.decode_png(payload)

    monkeypatch.setattr(visual, "MAX_PNG_CHUNKS", 4_096)
    monkeypatch.setattr(visual, "MAX_IDAT_BYTES", 1)
    with pytest.raises(visual.VisualBaselineError, match="png_idat_size_exceeded"):
        visual.decode_png(payload)


def test_verify_writes_ordered_candidate_bound_private_receipt_and_diffs(
    tmp_path: Path,
) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={
            "search-setup.mobile": _png(5, 8, (31, 34, 39, 255)),
            "public-home.desktop": _png(8, 5, (236, 232, 226, 255)),
        },
    )

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 0
    assert receipt["status"] == "pass"
    assert receipt["release_commit_sha"] == RELEASE_SHA
    assert receipt["expected_case_ids"] == [
        "search-setup.mobile",
        "public-home.desktop",
    ]
    assert receipt["observed_case_ids"] == receipt["expected_case_ids"]
    assert receipt["manifest"]["sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert len(receipt["manifest"]["git_blob_sha1"]) == 40
    assert receipt["browser"]["fingerprint_sha256"]
    assert all(row["baseline_sha256"] for row in receipt["outcomes"])
    assert all(row["actual_sha256"] for row in receipt["outcomes"])
    assert all(row["diff_sha256"] for row in receipt["outcomes"])
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in diff_dir.iterdir()
    )
    assert str(tmp_path) not in json.dumps(receipt)


def test_verify_fails_without_creating_missing_manifest_or_baseline(tmp_path: Path) -> None:
    missing_manifest = tmp_path / "missing" / "manifest.json"
    receipt_path = tmp_path / "evidence" / "receipt.json"
    receipt, exit_code = _verify(
        manifest_path=missing_manifest,
        actual_dir=tmp_path / "actuals",
        diff_dir=tmp_path / "diffs",
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert receipt["manifest"]["error"] == "manifest_missing"
    assert not missing_manifest.exists()
    assert not (tmp_path / "actuals").exists()

    manifest_path, actual_dir, diff_dir, second_receipt = _write_case_matrix(
        tmp_path / "baseline-missing",
        baseline_payloads={"results.desktop": _png(6, 4)},
    )
    baseline_path = manifest_path.parent / "images/results.desktop.png"
    baseline_path.unlink()
    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=second_receipt,
    )
    assert exit_code == 1
    assert "baseline_missing" in receipt["outcomes"][0]["reasons"]
    assert not baseline_path.exists()


def test_verify_fails_closed_for_tampered_baseline_and_candidate_mismatch(
    tmp_path: Path,
) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"research.desktop": _png(8, 6, (210, 200, 190, 255))},
    )
    (manifest_path.parent / "images/research.desktop.png").write_bytes(
        _png(8, 6, (12, 20, 28, 255))
    )

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        expected_release_sha="b" * 40,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert "baseline_sha256_mismatch" in receipt["outcomes"][0]["reasons"]
    assert next(
        check for check in receipt["checks"] if check["name"] == "candidate_sha_matches"
    )["ok"] is False


def test_verify_rejects_exact_dimension_drift_and_writes_dimension_diff(
    tmp_path: Path,
) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"empty.mobile": _png(5, 8)},
        actual_payloads={"empty.mobile": _png(6, 8)},
    )

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    outcome = receipt["outcomes"][0]
    assert "actual_dimension_mismatch" in outcome["reasons"]
    assert outcome["diff_sha256"]
    assert (diff_dir / "empty.mobile.diff.png").is_file()


def test_perceptual_threshold_allows_minor_antialias_noise(tmp_path: Path) -> None:
    baseline = _png(10, 10, (120, 120, 120, 255))
    antialias_noise = _png(10, 10, (124, 123, 121, 255))
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"search.mobile": baseline},
        actual_payloads={"search.mobile": antialias_noise},
        pixel_threshold=0.1,
        max_changed_pixel_ratio=visual.MAX_CHANGED_PIXEL_RATIO,
    )

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 0
    assert receipt["outcomes"][0]["changed_pixel_count"] == 0
    assert receipt["outcomes"][0]["changed_pixel_ratio"] == 0.0


def test_perceptual_ratio_rejects_deliberate_layout_shift(tmp_path: Path) -> None:
    width = 20
    height = 12
    background = bytearray(_rgba(width, height, (248, 246, 242, 255)))
    shifted = bytearray(background)
    for y in range(3, 9):
        for x in range(2, 8):
            offset = (y * width + x) * 4
            background[offset : offset + 4] = bytes((24, 28, 34, 255))
        for x in range(9, 15):
            offset = (y * width + x) * 4
            shifted[offset : offset + 4] = bytes((24, 28, 34, 255))
    baseline = visual.encode_rgba_png(width, height, bytes(background))
    actual = visual.encode_rgba_png(width, height, bytes(shifted))
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"workbench.desktop": baseline},
        actual_payloads={"workbench.desktop": actual},
        max_changed_pixel_ratio=0.005,
    )

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    outcome = receipt["outcomes"][0]
    assert "changed_pixel_ratio_exceeded" in outcome["reasons"]
    assert outcome["changed_pixel_ratio"] > 0.005


def test_manifest_schema_and_paths_are_strict() -> None:
    valid_case = {
        "id": "public-home.desktop",
        "baseline": "images/public-home.desktop.png",
        "width": 1440,
        "height": 1100,
        "sha256": "a" * 64,
    }
    payload = _manifest_payload([valid_case])
    payload["release_commit_sha"] = RELEASE_SHA
    with pytest.raises(visual.VisualBaselineError, match="manifest_keys_invalid"):
        visual.validate_manifest(payload)

    invalid_path = _manifest_payload(
        [{**valid_case, "baseline": "../outside.png"}]
    )
    with pytest.raises(
        visual.VisualBaselineError, match="manifest_baseline_path_invalid"
    ):
        visual.validate_manifest(invalid_path)


def test_manifest_rejects_coerced_types_and_duplicate_json_keys(tmp_path: Path) -> None:
    valid_case = {
        "id": "public-home.desktop",
        "baseline": "images/public-home.desktop.png",
        "width": 1440,
        "height": 1100,
        "sha256": "a" * 64,
    }

    boolean_version = _manifest_payload([valid_case])
    boolean_version["version"] = True
    with pytest.raises(visual.VisualBaselineError, match="manifest_schema_invalid"):
        visual.validate_manifest(boolean_version)

    boolean_scale = _manifest_payload([valid_case])
    boolean_scale["capture"] = {
        **visual.CAPTURE_CONTRACT,
        "device_scale_factor": True,
    }
    with pytest.raises(
        visual.VisualBaselineError, match="manifest_capture_contract_invalid"
    ):
        visual.validate_manifest(boolean_scale)

    string_threshold = _manifest_payload([valid_case])
    string_threshold["comparison"] = {
        **string_threshold["comparison"],
        "pixel_threshold": "0.1",
    }
    with pytest.raises(visual.VisualBaselineError, match="manifest_threshold_invalid"):
        visual.validate_manifest(string_threshold)

    relaxed_threshold = _manifest_payload([valid_case], pixel_threshold=0.2)
    with pytest.raises(visual.VisualBaselineError, match="manifest_threshold_invalid"):
        visual.validate_manifest(relaxed_threshold)

    relaxed_ratio = _manifest_payload([valid_case], max_changed_pixel_ratio=0.01)
    with pytest.raises(visual.VisualBaselineError, match="manifest_threshold_invalid"):
        visual.validate_manifest(relaxed_ratio)

    fractional_width = _manifest_payload([{**valid_case, "width": 1439.9}])
    with pytest.raises(
        visual.VisualBaselineError, match="manifest_case_dimensions_invalid"
    ):
        visual.validate_manifest(fractional_width)

    numeric_id = _manifest_payload([{**valid_case, "id": 1.2}])
    with pytest.raises(visual.VisualBaselineError, match="manifest_case_id_invalid"):
        visual.validate_manifest(numeric_id)

    manifest_path = tmp_path / "manifest.json"
    serialized = json.dumps(_manifest_payload([valid_case]))
    manifest_path.write_text(
        serialized.replace('"version": 1', '"version": 1, "version": 1', 1),
        encoding="utf-8",
    )
    with pytest.raises(
        visual.VisualBaselineError, match="manifest_json_duplicate_key"
    ):
        visual.load_manifest(manifest_path)


def test_verify_requires_exact_source_to_metadata_envelope_binding(tmp_path: Path) -> None:
    workflow_head_sha = "c" * 40
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"bound": _png(6, 4)},
    )
    binding = _source_binding_receipt(
        release_sha=RELEASE_SHA,
        workflow_head_sha=workflow_head_sha,
    )
    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        workflow_head_sha=workflow_head_sha,
        source_binding_receipt=binding,
    )
    assert exit_code == 0
    assert receipt["status"] == "pass"
    assert receipt["source_binding"] == binding
    assert next(
        check for check in receipt["checks"] if check["name"] == "source_checkout_bound"
    )["ok"] is True

    tampered_binding = dict(binding)
    tampered_binding["manifest_descendant_paths"] = [
        *visual.RELEASE_METADATA_DESCENDANT_PATHS,
        "ea/app/api/routes/landing.py",
    ]
    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        workflow_head_sha=workflow_head_sha,
        source_binding_receipt=tampered_binding,
    )
    assert exit_code == 1
    source_check = next(
        check for check in receipt["checks"] if check["name"] == "source_checkout_bound"
    )
    assert source_check["ok"] is False
    assert "source_binding_metadata_envelope_invalid" in source_check["errors"]


def test_verify_accepts_merge_aware_metadata_binding_parent(tmp_path: Path) -> None:
    workflow_head_sha = "c" * 40
    feature_parent_sha = "d" * 40
    base_parent_sha = "f" * 40
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"bound": _png(6, 4)},
    )
    binding = _source_binding_receipt(
        release_sha=RELEASE_SHA,
        workflow_head_sha=workflow_head_sha,
        binding_parent_sha=feature_parent_sha,
        merge_parent_shas=[base_parent_sha, feature_parent_sha],
    )

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        workflow_head_sha=workflow_head_sha,
        source_binding_receipt=binding,
    )

    assert exit_code == 0
    assert receipt["status"] == "pass"
    source_check = next(
        check for check in receipt["checks"] if check["name"] == "source_checkout_bound"
    )
    assert source_check["ok"] is True

    binding["parent_commit"] = workflow_head_sha
    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        workflow_head_sha=workflow_head_sha,
        source_binding_receipt=binding,
    )
    assert exit_code == 1
    source_check = next(
        check for check in receipt["checks"] if check["name"] == "source_checkout_bound"
    )
    assert "source_binding_metadata_envelope_invalid" in source_check["errors"]


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("merge_commit_required", True),
        ("merge_base_parent_commit", "c" * 40),
        ("reviewed_envelope_commit", "d" * 40),
        ("reviewed_envelope_parent_commits", ["a" * 40]),
        ("reviewed_envelope_tree", "e" * 40),
        ("merge_tree_matches_reviewed_envelope", True),
    ],
)
def test_verify_rejects_protected_source_binding_topology(
    tmp_path: Path,
    field: str,
    tampered_value: object,
) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"bound": _png(6, 4)},
    )
    binding = _source_binding_receipt()
    binding[field] = tampered_value

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        source_binding_receipt=binding,
    )

    assert exit_code == 1
    source_check = next(
        check for check in receipt["checks"] if check["name"] == "source_checkout_bound"
    )
    assert "source_binding_protected_topology_forbidden" in source_check["errors"]


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("merge_parent_commits", []),
        ("merge_parent_commits", ["b" * 40, "b" * 40]),
        ("merge_parent_commits", ["c" * 40]),
        ("merge_parent_commits", ["not-a-commit"]),
        ("merge_parent_commits", ["b" * 40, RELEASE_SHA]),
        ("merge_parent_commits", ["b" * 40, "c" * 40, "d" * 40]),
        ("head_tree", ""),
        ("head_tree", False),
    ],
)
def test_verify_rejects_invalid_default_source_binding_topology(
    tmp_path: Path,
    field: str,
    tampered_value: object,
) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"bound": _png(6, 4)},
    )
    binding = _source_binding_receipt()
    binding[field] = tampered_value

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
        source_binding_receipt=binding,
    )

    assert exit_code == 1
    source_check = next(
        check for check in receipt["checks"] if check["name"] == "source_checkout_bound"
    )
    assert "source_binding_topology_invalid" in source_check["errors"]


def test_verify_rejects_diff_baseline_path_collision_without_mutation(
    tmp_path: Path,
) -> None:
    manifest_path, actual_dir, _diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"case": _png(6, 4, (20, 30, 40, 255))},
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_path = manifest_path.parent / "images/case.png"
    collision_path = manifest_path.parent / "images/case.diff.png"
    original_payload = original_path.read_bytes()
    original_path.rename(collision_path)
    manifest["cases"][0]["baseline"] = "images/case.diff.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=manifest_path.parent / "images",
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert receipt_path.is_file()
    assert collision_path.read_bytes() == original_payload
    assert any(
        error.startswith("path_graph_collision:baseline:case:diff:case")
        for error in receipt["preflight"]["errors"]
    )
    unchanged_check = next(
        check
        for check in receipt["checks"]
        if check["name"] == "verify_did_not_update_baselines"
    )
    assert unchanged_check["ok"] is False


def test_verify_rejects_hardlink_inode_collision(tmp_path: Path) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"hardlink": _png(6, 4)},
    )
    baseline_path = manifest_path.parent / "images/hardlink.png"
    actual_path = actual_dir / "hardlink.png"
    actual_path.unlink()
    actual_path.hardlink_to(baseline_path)
    before = baseline_path.read_bytes()

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert baseline_path.read_bytes() == before
    assert "path_graph_collision" in receipt["outcomes"][0]["reasons"]


def test_receipt_path_cannot_overwrite_a_baseline(tmp_path: Path) -> None:
    manifest_path, actual_dir, diff_dir, _receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"protected": _png(6, 4)},
    )
    baseline_path = manifest_path.parent / "images/protected.png"
    before = baseline_path.read_bytes()

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=baseline_path,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert receipt["receipt_written"] is False
    assert baseline_path.read_bytes() == before
    assert next(
        check for check in receipt["checks"] if check["name"] == "receipt_path_safe"
    )["ok"] is False


def test_symlink_escape_produces_structured_failing_receipt(tmp_path: Path) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"research": _png(6, 4)},
    )
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png(6, 4))
    actual_path = actual_dir / "research.png"
    actual_path.unlink()
    actual_path.symlink_to(outside)

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert receipt["receipt_written"] is True
    assert receipt_path.is_file()
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert {
        "actual_path_escape",
        "actual_symlink_forbidden",
    } & set(receipt["outcomes"][0]["reasons"])


def test_verify_rejects_extra_actual_png(tmp_path: Path) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"search": _png(6, 4)},
    )
    (actual_dir / "unapproved-extra.png").write_bytes(_png(6, 4))

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert receipt["preflight"]["extra_actual_pngs"] == ["unapproved-extra.png"]
    assert next(
        check for check in receipt["checks"] if check["name"] == "exact_actual_png_set"
    )["ok"] is False


def test_missing_actual_cleans_expected_stale_diff_safely(tmp_path: Path) -> None:
    manifest_path, actual_dir, diff_dir, receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"offline": _png(6, 4)},
    )
    actual_dir.joinpath("offline.png").unlink()
    diff_dir.mkdir(parents=True, exist_ok=True)
    stale_diff = diff_dir / "offline.diff.png"
    stale_diff.write_bytes(_png(6, 4, (220, 20, 40, 255)))

    receipt, exit_code = _verify(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        diff_dir=diff_dir,
        receipt_path=receipt_path,
    )

    assert exit_code == 1
    assert receipt["status"] == "fail"
    assert "actual_missing" in receipt["outcomes"][0]["reasons"]
    assert not stale_diff.exists()


def test_explicit_update_is_forbidden_in_ci_and_updates_only_when_local(
    tmp_path: Path,
) -> None:
    original = _png(7, 5, (210, 205, 200, 255))
    replacement = _png(7, 5, (60, 80, 100, 255))
    manifest_path, actual_dir, _diff_dir, _receipt_path = _write_case_matrix(
        tmp_path,
        baseline_payloads={"offline.desktop": original},
        actual_payloads={"offline.desktop": replacement},
    )
    baseline_path = manifest_path.parent / "images/offline.desktop.png"
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(
        visual.VisualBaselineError, match="baseline_update_forbidden_in_ci"
    ):
        visual.update_baselines(
            manifest_path=manifest_path,
            actual_dir=actual_dir,
            environ={"CI": "true"},
        )
    assert baseline_path.read_bytes() == original
    assert manifest_path.read_bytes() == manifest_before

    result = visual.update_baselines(
        manifest_path=manifest_path,
        actual_dir=actual_dir,
        environ={"CI": "false", "GITHUB_ACTIONS": "0"},
    )
    updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["status"] == "updated"
    assert baseline_path.read_bytes() == replacement
    assert updated_manifest["cases"][0]["sha256"] == hashlib.sha256(
        replacement
    ).hexdigest()
    assert "release_commit_sha" not in updated_manifest
