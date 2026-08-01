from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
APK_DIRECTORY = ROOT / "vendor" / "propertyquarry-render-apks" / "alpine-3.24-x86_64"
HASH_LOCK = ROOT / "ea" / "render-system-packages.sha256"
METADATA_LOCK = ROOT / "ea" / "render-system-packages.lock.json"
REQUIREMENTS_LOCK = ROOT / "ea" / "requirements.render.lock"
DOCKERFILE = ROOT / "ea" / "Dockerfile.property-render"
COMPOSE_FILE = ROOT / "docker-compose.property.yml"
PYTHON_ALPINE_X86_64_MANIFEST = (
    "sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
)
PILLOW_WHEEL = "pillow-12.3.0-cp312-cp312-musllinux_1_2_x86_64.whl"
PILLOW_SHA256 = "0dd2064cbc55aaec028ef5fbb60fa47bb6c3e7918e07ff17935284b227a9d2df"
PSYCOPG_WHEEL = "psycopg-3.3.4-py3-none-any.whl"
PSYCOPG_SHA256 = "b6bbc25ccf05c8fad3b061d9db2ef0909a555171b84b07f29458a447253d679a"
PSYCOPG_BINARY_WHEEL = (
    "psycopg_binary-3.3.4-cp312-cp312-musllinux_1_2_x86_64.whl"
)
PSYCOPG_BINARY_SHA256 = (
    "71e55ccbdfae79a2ed9c6369c3008a3025817ff9d7e27b32a2d84e2a4267e66e"
)
TYPING_EXTENSIONS_WHEEL = "typing_extensions-4.15.0-py3-none-any.whl"
TYPING_EXTENSIONS_SHA256 = (
    "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548"
)
HASH_LINE = re.compile(r"(?P<digest>[0-9a-f]{64})  (?P<filename>[^\s/\\]+\.apk)")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_inventory() -> dict[str, str]:
    observed: dict[str, str] = {}
    for line in HASH_LOCK.read_text(encoding="utf-8").splitlines():
        match = HASH_LINE.fullmatch(line)
        assert match is not None
        filename = match.group("filename")
        assert filename not in observed
        observed[filename] = match.group("digest")
    return observed


def test_render_alpine_artifacts_are_exact_safe_and_hash_locked() -> None:
    metadata = json.loads(METADATA_LOCK.read_text(encoding="utf-8"))
    expected = _hash_inventory()
    paths = sorted(APK_DIRECTORY.iterdir())

    assert metadata["schema"] == "propertyquarry.render_system_packages.v1"
    assert metadata["architecture"] == "x86_64"
    assert metadata["base_image"] == {
        "name": "docker.io/library/python:3.12-alpine",
        "manifest_digest": PYTHON_ALPINE_X86_64_MANIFEST,
        "os": "alpine",
        "suite": "3.24",
        "version": "3.24.1",
    }
    assert metadata["artifacts"] == {
        "count": 114,
        "aggregate_bytes": 53557077,
        "directory": "vendor/propertyquarry-render-apks/alpine-3.24-x86_64",
        "sha256_lock": "ea/render-system-packages.sha256",
    }
    assert len(paths) == len(expected) == metadata["artifacts"]["count"]
    assert sum(path.stat().st_size for path in paths) == metadata["artifacts"]["aggregate_bytes"]
    assert [path.name for path in paths] == sorted(expected)

    for path in paths:
        file_metadata = path.lstat()
        assert stat.S_ISREG(file_metadata.st_mode)
        assert not stat.S_ISLNK(file_metadata.st_mode)
        assert stat.S_IMODE(file_metadata.st_mode) == 0o644
        assert file_metadata.st_nlink == 1
        assert path.name == path.name.strip()
        assert not any(character.isspace() for character in path.name)
        assert _hash_file(path) == expected[path.name]

    filenames = set(expected)
    assert "ffmpeg-8.1.2-r0.apk" in filenames
    assert "ffmpeg-libavcodec-8.1.2-r0.apk" in filenames
    assert "ffmpeg-libavformat-8.1.2-r0.apk" in filenames
    for forbidden in ("blender", "colmap", "imagemagick", "meshlab", "numpy"):
        assert not any(forbidden in filename.lower() for filename in filenames)


def test_render_python_dependencies_are_exact_vendored_hash_locked_wheels() -> None:
    wheelhouse = (
        ROOT
        / "vendor"
        / "propertyquarry-wheelhouse"
        / "cp312-musllinux-x86_64"
    )
    expected_wheels = {
        PILLOW_WHEEL: PILLOW_SHA256,
        PSYCOPG_WHEEL: PSYCOPG_SHA256,
        PSYCOPG_BINARY_WHEEL: PSYCOPG_BINARY_SHA256,
        TYPING_EXTENSIONS_WHEEL: TYPING_EXTENSIONS_SHA256,
    }
    requirement_lines = [
        line
        for line in REQUIREMENTS_LOCK.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    assert requirement_lines == [
        f"pillow==12.3.0 --hash=sha256:{PILLOW_SHA256}",
        f"psycopg==3.3.4 --hash=sha256:{PSYCOPG_SHA256}",
        f"psycopg-binary==3.3.4 --hash=sha256:{PSYCOPG_BINARY_SHA256}",
        (
            "typing-extensions==4.15.0 "
            f"--hash=sha256:{TYPING_EXTENSIONS_SHA256}"
        ),
    ]
    assert {path.name for path in wheelhouse.iterdir()} == set(expected_wheels)
    for filename, expected_sha256 in expected_wheels.items():
        wheel = wheelhouse / filename
        assert wheel.is_file()
        assert _hash_file(wheel) == expected_sha256


def test_render_image_is_offline_minimal_non_root_and_immutable() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.startswith("FROM ")
    ]

    assert from_lines == [
        f"FROM python:3.12-alpine@{PYTHON_ALPINE_X86_64_MANIFEST} AS assembly",
        "FROM scratch AS runtime",
    ]
    assert dockerfile.count("http://") == 1
    assert "http://127.0.0.1:8091/health/ready" in dockerfile
    assert "https://" not in dockerfile
    assert "apk update" not in dockerfile
    assert "apk fetch" not in dockerfile
    assert "apt-get update" not in dockerfile
    assert "apt-get download" not in dockerfile
    assert ": > /etc/apk/repositories" in dockerfile
    assert "apk --no-network --no-cache add /tmp/render-packages/*.apk" in dockerfile
    assert "rm -f /sbin/apk" in dockerfile
    assert "/usr/bin/apt-get" not in dockerfile
    assert "python -m playwright" not in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH" not in dockerfile
    assert "/ms-playwright" not in dockerfile
    assert "requirements.txt" not in dockerfile
    assert "requirements.lock" not in dockerfile
    assert "COPY ea/app /app/app" not in dockerfile
    assert dockerfile.count("ea/app/") == 3
    assert (
        "COPY --chown=10001:10001 ea/app/observability.py "
        "/app/app/observability.py"
        in dockerfile
    )
    assert (
        "COPY --chown=10001:10001 ea/app/services/admission_control.py "
        "/app/app/services/admission_control.py"
        in dockerfile
    )
    assert (
        "COPY --chown=10001:10001 ea/app/product/property_diorama_preview.py "
        "/app/app/product/property_diorama_preview.py"
        in dockerfile
    )
    assert "python -m compileall -q /app/scripts /app/app" in dockerfile
    assert "docker-entrypoint" not in dockerfile
    assert "colmap" not in dockerfile
    assert "meshlab" not in dockerfile
    assert "exiftool" not in dockerfile
    assert "imagemagick" not in dockerfile
    assert "numpy" not in dockerfile
    assert "command -v blender >/dev/null" not in dockerfile
    assert "test -z \"$(command -v blender || true)\"" in dockerfile

    assert "sha256sum -cs" in dockerfile
    assert dockerfile.index("sha256sum -cs") < dockerfile.index(
        "apk --no-network --no-cache add"
    )
    assert "dpkg" not in dockerfile
    assert "--no-index" in dockerfile
    assert "--require-hashes" in dockerfile
    assert f"/{PILLOW_WHEEL} /tmp/render-wheelhouse/" in dockerfile
    assert "python -m pip uninstall --yes pip" in dockerfile
    assert "find_spec('pip') is None" in dockerfile
    assert "find_spec('playwright') is None" in dockerfile

    copied_scripts = {
        match.group(1)
        for match in re.finditer(
            r"^COPY --chown=10001:10001 scripts/([^ ]+) ",
            dockerfile,
            flags=re.MULTILINE,
        )
    }
    assert copied_scripts == {
        "generate_property_reconstruction.py",
        "property_reconstruction_styles.py",
        "property_tour_layout_contract.py",
        "property_reconstruction_render_bridge.py",
        "property_tour_governed_reservation.py",
        "property_tour_runtime_paths.py",
        "propertyquarry_playwright_runtime.py",
    }

    runtime = dockerfile.split("FROM scratch AS runtime", 1)[1]
    assert "\nRUN " not in runtime
    assert "\nENTRYPOINT " not in runtime
    assert "COPY --from=assembly / /" in runtime
    assert "USER 10001:10001" in runtime
    assert "ProxyHandler({})" in runtime
    assert "shutil.which('blender') is None" in runtime
    assert "/health/ready" in runtime
    assert (
        'CMD ["/usr/local/bin/python", '
        '"/app/scripts/property_reconstruction_render_bridge.py"]'
        in runtime
    )


def test_render_compose_inherits_image_command_and_limits_runtime_authority() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = compose["services"]["propertyquarry-render-tools"]
    environment = service["environment"]

    assert service["build"] == {
        "context": ".",
        "dockerfile": "ea/Dockerfile.property-render",
    }
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["read_only"] is True
    assert "command" not in service
    assert "entrypoint" not in service
    assert "healthcheck" not in service
    assert service["volumes"] == [
        "propertyquarry_artifacts:/data/artifacts",
        "propertyquarry_public_tours:/data/public_property_tours",
    ]
    assert service["tmpfs"] == [
        "/tmp:size=${PROPERTYQUARRY_RENDER_TMPFS_LIMIT:-512m},mode=1777",
    ]
    assert environment["HOME"] == "/tmp/home"
    assert environment["XDG_CACHE_HOME"] == "/tmp/cache"
    assert environment["XDG_CONFIG_HOME"] == "/tmp/config"
    assert environment["TMPDIR"] == "/tmp"
    assert environment["DATABASE_URL"].startswith(
        "${PROPERTYQUARRY_RENDER_DATABASE_URL:?"
    )
    assert environment["PROPERTYQUARRY_ADMISSION_BACKEND"] == "postgres"
    assert environment["PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET"] == ""
    assert environment["THREEDVISTA_LOGIN_EMAIL"] == ""
    assert environment["THREEDVISTA_LOGIN_PASSWORD"] == ""
    assert environment["THREEDVISTA_LICENSE_EMAIL"] == ""
    assert "EA_ARTIFACTS_DIR" not in environment
    assert "EA_RESPONSES_PROVIDER_LEDGER_DIR" not in environment
    assert "PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR" not in environment
    assert "TEABLE_BASE_URL" not in environment
