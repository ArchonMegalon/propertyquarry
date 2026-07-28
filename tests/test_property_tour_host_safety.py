from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import property_tour_host_safety as safety


def test_free_disk_guard_defaults_to_ten_gib_and_honors_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_TOUR_MIN_FREE_BYTES", raising=False)
    monkeypatch.setattr(
        safety.shutil,
        "disk_usage",
        lambda _path: safety.shutil._ntuple_diskusage(20, 14, 6),
    )

    with pytest.raises(safety.TourHostSafetyError, match="tour_import_low_disk"):
        safety.require_free_disk(tmp_path, reason_prefix="tour_import")

    monkeypatch.setenv("PROPERTYQUARRY_TOUR_MIN_FREE_BYTES", "5")
    receipt = safety.require_free_disk(tmp_path, reason_prefix="tour_import")
    assert receipt == {
        "free_bytes": 6,
        "minimum_free_bytes": 5,
        "expected_write_bytes": 0,
    }


def test_bounded_tree_rejects_symlinks_and_file_budget_overflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    root.mkdir()
    (root / "one.bin").write_bytes(b"1")
    (root / "two.bin").write_bytes(b"2")

    with pytest.raises(safety.TourHostSafetyError, match="file_count_limit"):
        safety.require_bounded_tree(
            root,
            reason_prefix="tour_export",
            maximum_files=1,
            maximum_total_bytes=100,
            maximum_file_bytes=100,
        )

    (root / "two.bin").unlink()
    (root / "link.bin").symlink_to(root / "one.bin")
    with pytest.raises(safety.TourHostSafetyError, match="symlink_forbidden"):
        safety.require_bounded_tree(root, reason_prefix="tour_export")


def test_safe_zip_extraction_rejects_compression_bombs_and_cleans_no_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_TOUR_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("PROPERTYQUARRY_TOUR_EXPORT_MAX_EXPANDED_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("PROPERTYQUARRY_TOUR_ARCHIVE_MAX_COMPRESSION_RATIO", "2")
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("export/zeros.bin", b"0" * (1024 * 1024))
    target = tmp_path / "expanded"

    with pytest.raises(safety.TourHostSafetyError, match="compression_ratio_limit"):
        safety.safe_extract_tour_zip(
            archive_path,
            target,
            reason_prefix="tour_zip",
        )

    assert not any(path.is_file() for path in target.rglob("*"))


def test_bounded_subprocess_kills_timeout_and_output_overflow(tmp_path: Path) -> None:
    env = dict(os.environ)
    with pytest.raises(safety.TourHostSafetyError, match="subprocess_timeout"):
        safety.run_bounded_subprocess(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=env,
            timeout_seconds=1,
            maximum_output_bytes=1024,
        )

    with pytest.raises(safety.TourHostSafetyError, match="subprocess_output_limit"):
        safety.run_bounded_subprocess(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
            cwd=tmp_path,
            env=env,
            timeout_seconds=5,
            maximum_output_bytes=1024,
        )


def test_lane_lock_rejects_concurrent_process_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_TOUR_LOCK_DIR", str(tmp_path / "locks"))
    with safety.bounded_lane_lock("magicfit-render"):
        with pytest.raises(safety.TourHostSafetyError, match="concurrency_limit_reached"):
            with safety.bounded_lane_lock("magicfit-render"):
                pass
