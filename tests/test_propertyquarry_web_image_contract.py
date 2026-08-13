from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "ea" / "Dockerfile.property-web"
COMPOSE_FILE = ROOT / "docker-compose.property.yml"
SHA256 = r"sha256:[0-9a-f]{64}"
PYTHON_AMD64_MANIFEST = (
    "sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464"
)
DISTROLESS_AMD64_MANIFEST = (
    "sha256:d0b79eb697888ecb8ef019bbb7192e4f41974830ea95f0543123eaaeb2d5fd2c"
)


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_web_image_uses_two_immutable_stages_and_a_distroless_runtime() -> None:
    dockerfile = _dockerfile()
    from_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.startswith("FROM ")
    ]

    assert len(from_lines) == 2
    assert re.fullmatch(
        rf"FROM python:3\.12-slim@{SHA256} AS build",
        from_lines[0],
    )
    assert re.fullmatch(
        rf"FROM gcr\.io/distroless/cc-debian13@{SHA256} AS runtime",
        from_lines[1],
    )
    assert from_lines == [
        f"FROM python:3.12-slim@{PYTHON_AMD64_MANIFEST} AS build",
        (
            "FROM gcr.io/distroless/cc-debian13@"
            f"{DISTROLESS_AMD64_MANIFEST} AS runtime"
        ),
    ]

    runtime = dockerfile.split(from_lines[1], 1)[1]
    assert "\nRUN " not in runtime
    assert (
        'ENTRYPOINT ["/usr/local/bin/python", "-I", "-S", '
        '"/usr/local/libexec/property_web_entrypoint.py"]'
        in runtime
    )
    assert "/bin/sh" not in runtime
    assert "curl" not in runtime
    assert "apt-get" not in runtime
    assert "USER 10001:10001" in runtime
    assert 'CMD ["python", "-m", "app.runner"]' in runtime
    assert "ProxyHandler({})" in runtime


def test_web_runtime_copies_only_scanner_visible_native_library_packages() -> None:
    dockerfile = _dockerfile()
    package_loop = re.search(
        r"for package in (?P<packages>[^;]+); do",
        dockerfile,
    )

    assert package_loop is not None
    assert tuple(package_loop.group("packages").split()) == (
        "libbz2-1.0",
        "libcrypt1",
        "libffi8",
        "liblzma5",
        "libsqlite3-0",
    )
    assert 'dpkg-query --status "${package}"' in dockerfile
    assert (
        '"${runtime_overlay}/var/lib/dpkg/status.d/${package}"'
        in dockerfile
    )
    assert "tar --create --no-recursion" in dockerfile
    assert '&& [ "${package_path}" != "/." ]' in dockerfile
    assert "COPY --from=build /runtime-overlay/ /" in dockerfile


def test_web_runtime_excludes_package_managers_and_shell_entrypoint() -> None:
    dockerfile = _dockerfile()

    assert "python3-numpy" not in dockerfile
    assert "apt-get" not in dockerfile
    assert "COPY ea/docker-entrypoint.sh" not in dockerfile
    assert "python -m pip uninstall --yes pip" in dockerfile
    assert "COPY --from=build /usr/local /usr/local" in dockerfile


def test_compose_command_override_remains_compatible_with_exec_entrypoint() -> None:
    dockerfile = _dockerfile()
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert (
        'COPY --chmod=0555 scripts/property_web_entrypoint.py '
        '/usr/local/libexec/property_web_entrypoint.py'
        in dockerfile
    )
    assert (
        'ENTRYPOINT ["/usr/local/bin/python", "-I", "-S", '
        '"/usr/local/libexec/property_web_entrypoint.py"]'
        in dockerfile
    )
    assert 'CMD ["python", "-m", "app.runner"]' in dockerfile
    assert (
        'command: ["/usr/local/bin/python", "-m", "app.product.propertyquarry_schema", "migrate"]'
        in compose
    )
