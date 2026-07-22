from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "ea" / "Dockerfile.property"
FETCH_SCRIPT = ROOT / "ea" / "property_render_ffmpeg_source_fetch.sh"
BUILD_RECIPE = ROOT / "ea" / "property_render_ffmpeg_build_recipe.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fetch_env(tmp_path: Path, *, wget_body: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    attempts = tmp_path / "attempts"
    _write_executable(fake_bin / "wget", wget_body)
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
    )
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FETCH_ATTEMPT_LOG"] = str(attempts)
    return env, attempts


def test_render_dockerfile_fetches_ffmpeg_sources_sequentially() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "https://ffmpeg.org/" not in dockerfile
    assert "ea/property_render_ffmpeg_source_fetch.sh" in dockerfile
    fetch_step = "RUN /usr/local/libexec/property_render_ffmpeg_source_fetch.sh"
    x264_step = "https://code.videolan.org/videolan/x264/"
    recipe_step = "RUN /usr/local/libexec/property_render_ffmpeg_build_recipe.sh"
    assert dockerfile.index(fetch_step) < dockerfile.index(x264_step)
    assert dockerfile.index(x264_step) < dockerfile.index(recipe_step)


def test_ffmpeg_fetch_contract_preserves_hash_and_signature_pins() -> None:
    fetch = FETCH_SCRIPT.read_text(encoding="utf-8")
    recipe = BUILD_RECIPE.read_text(encoding="utf-8")
    expected_hashes = {
        "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c",
        "0a0963fccd70597838073f3e31b20f4a4d8cc2b5e577472c9a5a1f22624246f8",
        "397b3becedcd5a98769967ff1ff8501ddc89f8368b8f766e4701377d7dbaabe5",
    }

    for expected_hash in expected_hashes:
        assert expected_hash in fetch
        assert expected_hash in recipe
    assert 'readonly DOWNLOAD_ATTEMPTS=5' in fetch
    assert 'readonly DOWNLOAD_TIMEOUT_SECONDS=120' in fetch
    assert '"${partial}" | sha256sum -c -' in fetch
    assert "trap cleanup_active_partial EXIT" in fetch
    assert "trap 'exit 143' TERM" in fetch
    assert 'mv -- "${partial}" "${destination}"' in fetch
    assert "gpg --batch --verify" in recipe
    subprocess.run(["bash", "-n", str(FETCH_SCRIPT)], check=True)


def test_ffmpeg_fetch_retries_transport_failure_and_removes_partial_file(
    tmp_path: Path,
) -> None:
    env, attempts = _fetch_env(
        tmp_path,
        wget_body="""#!/usr/bin/env bash
set -euo pipefail
printf x >> "${FETCH_ATTEMPT_LOG:?}"
destination=""
while (( $# )); do
    if [[ "$1" == "-O" ]]; then
        destination="$2"
        shift 2
    else
        shift
    fi
done
printf partial > "${destination:?}"
exit 1
""",
    )
    sources = tmp_path / "sources"

    result = subprocess.run(
        ["bash", str(FETCH_SCRIPT), str(sources)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert attempts.read_text(encoding="utf-8") == "xxxxx"
    assert not any(path.name.endswith(".part") for path in sources.iterdir())
    assert not (sources / "ffmpeg-8.1.2.tar.xz").exists()


def test_ffmpeg_fetch_fails_closed_without_retrying_checksum_mismatch(
    tmp_path: Path,
) -> None:
    env, attempts = _fetch_env(
        tmp_path,
        wget_body="""#!/usr/bin/env bash
set -euo pipefail
printf x >> "${FETCH_ATTEMPT_LOG:?}"
destination=""
while (( $# )); do
    if [[ "$1" == "-O" ]]; then
        destination="$2"
        shift 2
    else
        shift
    fi
done
printf wrong-source-bytes > "${destination:?}"
""",
    )
    sources = tmp_path / "sources"

    result = subprocess.run(
        ["bash", str(FETCH_SCRIPT), str(sources)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert attempts.read_text(encoding="utf-8") == "x"
    assert "source checksum mismatch" in result.stderr
    assert not any(path.name.endswith(".part") for path in sources.iterdir())
    assert not (sources / "ffmpeg-8.1.2.tar.xz").exists()


def test_ffmpeg_fetch_cleans_partial_when_atomic_promotion_fails(
    tmp_path: Path,
) -> None:
    env, attempts = _fetch_env(
        tmp_path,
        wget_body="""#!/usr/bin/env bash
set -euo pipefail
printf x >> "${FETCH_ATTEMPT_LOG:?}"
destination=""
while (( $# )); do
    if [[ "$1" == "-O" ]]; then
        destination="$2"
        shift 2
    else
        shift
    fi
done
printf verified-source-bytes > "${destination:?}"
""",
    )
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    _write_executable(
        fake_bin / "sha256sum",
        "#!/usr/bin/env bash\nset -euo pipefail\ncat >/dev/null\nexit 0\n",
    )
    _write_executable(
        fake_bin / "mv",
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 1\n",
    )
    sources = tmp_path / "sources"

    result = subprocess.run(
        ["bash", str(FETCH_SCRIPT), str(sources)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert attempts.read_text(encoding="utf-8") == "x"
    assert not any(path.name.endswith(".part") for path in sources.iterdir())
    assert not (sources / "ffmpeg-8.1.2.tar.xz").exists()
