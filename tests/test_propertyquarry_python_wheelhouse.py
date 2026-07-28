from __future__ import annotations

from pathlib import Path

from scripts.verify_propertyquarry_python_wheelhouse import verify_wheelhouse


ROOT = Path(__file__).resolve().parents[1]


def test_propertyquarry_python_wheelhouse_is_exact_and_hash_locked() -> None:
    result = verify_wheelhouse(
        requirements_lock=ROOT / "ea" / "requirements.lock",
        hash_lock=ROOT / "ea" / "requirements.wheelhouse.lock",
        wheelhouse=(
            ROOT / "vendor" / "propertyquarry-wheelhouse" / "cp312-linux-x86_64"
        ),
    )

    assert result["schema"] == "propertyquarry.python_wheelhouse.v1"
    assert result["wheel_count"] == 37
    assert int(result["total_bytes"]) > 80_000_000
    assert len(str(result["aggregate_sha256"])) == 64


def test_property_web_dockerfile_installs_only_from_verified_offline_wheels() -> None:
    dockerfile = (ROOT / "ea" / "Dockerfile.property-web").read_text(
        encoding="utf-8"
    )

    assert "apt-get update" not in dockerfile
    assert "--network=host" not in dockerfile
    assert (
        "COPY vendor/propertyquarry-wheelhouse/cp312-linux-x86_64 /wheelhouse"
        in dockerfile
    )
    assert "verify_propertyquarry_python_wheelhouse.py" in dockerfile
    assert "--no-index" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--find-links=/wheelhouse" in dockerfile
