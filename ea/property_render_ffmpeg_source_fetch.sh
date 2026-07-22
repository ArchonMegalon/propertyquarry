#!/usr/bin/env bash
set -euo pipefail

readonly DOWNLOAD_ATTEMPTS=5
readonly DOWNLOAD_TIMEOUT_SECONDS=120
readonly FFMPEG_URL="https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz"
readonly FFMPEG_SHA256="464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
readonly FFMPEG_SIGNATURE_URL="https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz.asc"
readonly FFMPEG_SIGNATURE_SHA256="0a0963fccd70597838073f3e31b20f4a4d8cc2b5e577472c9a5a1f22624246f8"
readonly FFMPEG_KEY_URL="https://ffmpeg.org/ffmpeg-devel.asc"
readonly FFMPEG_KEY_SHA256="397b3becedcd5a98769967ff1ff8501ddc89f8368b8f766e4701377d7dbaabe5"
ACTIVE_PARTIAL=""

cleanup_active_partial() {
    if [[ -n "${ACTIVE_PARTIAL}" ]]; then
        rm -f -- "${ACTIVE_PARTIAL}" || true
    fi
}

trap cleanup_active_partial EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if (( $# > 1 )); then
    printf 'usage: %s [sources-dir]\n' "$0" >&2
    exit 2
fi

readonly SOURCES_DIR="${1:-/sources}"
if [[ -L "${SOURCES_DIR}" ]]; then
    printf 'source directory must not be a symlink: %s\n' "${SOURCES_DIR}" >&2
    exit 1
fi
install -d -m 0755 "${SOURCES_DIR}"
if [[ ! -d "${SOURCES_DIR}" || -L "${SOURCES_DIR}" ]]; then
    printf 'source directory boundary is invalid: %s\n' "${SOURCES_DIR}" >&2
    exit 1
fi

download_source() {
    local url="$1"
    local expected_sha256="$2"
    local destination="$3"
    local partial="${destination}.part"
    local attempt=1

    ACTIVE_PARTIAL="${partial}"
    rm -f -- "${destination}" "${partial}"
    while (( attempt <= DOWNLOAD_ATTEMPTS )); do
        if wget -T "${DOWNLOAD_TIMEOUT_SECONDS}" -O "${partial}" "${url}"; then
            if ! printf '%s  %s\n' "${expected_sha256}" "${partial}" | sha256sum -c -; then
                rm -f -- "${partial}"
                printf 'source checksum mismatch: %s\n' "${url}" >&2
                return 1
            fi
            chmod 0444 "${partial}"
            mv -- "${partial}" "${destination}"
            ACTIVE_PARTIAL=""
            return 0
        fi

        rm -f -- "${partial}"
        if (( attempt == DOWNLOAD_ATTEMPTS )); then
            printf 'source download failed after %s attempts: %s\n' \
                "${DOWNLOAD_ATTEMPTS}" "${url}" >&2
            return 1
        fi
        sleep "$((attempt * 3))"
        ((attempt += 1))
    done
}

download_source \
    "${FFMPEG_URL}" \
    "${FFMPEG_SHA256}" \
    "${SOURCES_DIR}/ffmpeg-8.1.2.tar.xz"
download_source \
    "${FFMPEG_SIGNATURE_URL}" \
    "${FFMPEG_SIGNATURE_SHA256}" \
    "${SOURCES_DIR}/ffmpeg-8.1.2.tar.xz.asc"
download_source \
    "${FFMPEG_KEY_URL}" \
    "${FFMPEG_KEY_SHA256}" \
    "${SOURCES_DIR}/ffmpeg-devel.asc"
