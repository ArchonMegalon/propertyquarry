from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE_ROOT = REPO_ROOT / "docker" / "propertyquarry-playwright"
EXPECTED_VERSION = "1.62.1"
EXPECTED_BASE_DIGEST = "sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e"


def test_playwright_worker_uses_one_exact_patched_version() -> None:
    package = json.loads((IMAGE_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((IMAGE_ROOT / "package-lock.json").read_text(encoding="utf-8"))

    assert package["dependencies"]["playwright"] == EXPECTED_VERSION
    assert lock["packages"][""]["dependencies"]["playwright"] == EXPECTED_VERSION
    assert lock["packages"]["node_modules/playwright"]["version"] == EXPECTED_VERSION
    assert lock["packages"]["node_modules/playwright-core"]["version"] == EXPECTED_VERSION


def test_playwright_worker_base_and_build_are_reproducibly_pinned() -> None:
    dockerfile = (IMAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
    build_script = (REPO_ROOT / "scripts" / "build_propertyquarry_playwright_image.sh").read_text(encoding="utf-8")

    assert f"v{EXPECTED_VERSION}-noble@{EXPECTED_BASE_DIGEST}" in dockerfile
    assert "npm ci --omit=dev --ignore-scripts" in dockerfile
    assert 'EXPECTED_VERSION="1.62.1"' in build_script
    assert "npm audit --omit=dev --audit-level=high" in build_script
    assert "chromium.launch({headless:true})" in build_script
