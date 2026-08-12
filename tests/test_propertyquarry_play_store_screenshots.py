from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from scripts.propertyquarry_play_store_screenshots import (
    MAX_PLAY_STORE_IMAGE_BYTES,
    _atomic_write_private_json,
    png_dimensions,
    validate_play_store_screenshot,
)


def _write_minimal_png(path: Path, *, width: int, height: int) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def test_validate_play_store_screenshot_accepts_exact_phone_dimensions(tmp_path: Path) -> None:
    screenshot = tmp_path / "phone.png"
    _write_minimal_png(screenshot, width=1080, height=1920)

    receipt = validate_play_store_screenshot(screenshot)

    assert receipt["width"] == 1080
    assert receipt["height"] == 1920
    assert receipt["size_bytes"] == screenshot.stat().st_size
    assert len(str(receipt["sha256"])) == 64


@pytest.mark.parametrize(
    ("width", "height"),
    ((1079, 1920), (1080, 1919), (1920, 1080)),
)
def test_validate_play_store_screenshot_rejects_wrong_dimensions(
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    screenshot = tmp_path / "phone.png"
    _write_minimal_png(screenshot, width=width, height=height)

    with pytest.raises(ValueError, match="dimensions_invalid"):
        validate_play_store_screenshot(screenshot)


def test_png_dimensions_rejects_non_png(tmp_path: Path) -> None:
    screenshot = tmp_path / "phone.png"
    screenshot.write_bytes(b"not-a-png")

    with pytest.raises(ValueError, match="not_png"):
        png_dimensions(screenshot)


def test_private_receipt_write_is_atomic_and_owner_only(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"

    _atomic_write_private_json(receipt, {"status": "pass"})

    assert json.loads(receipt.read_text(encoding="utf-8")) == {"status": "pass"}
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_play_store_limit_stays_at_eight_megabytes() -> None:
    assert MAX_PLAY_STORE_IMAGE_BYTES == 8 * 1024 * 1024
