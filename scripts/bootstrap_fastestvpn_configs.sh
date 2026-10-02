#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${ROOT_DIR}/vpn/fastestvpn"
ARCHIVE_URL="${FASTESTVPN_CONFIG_ARCHIVE_URL:-http://support.fastestvpn.com/download/fastestvpn_ovpn/}"
TMP_ARCHIVE="$(mktemp "${TMPDIR:-/tmp}/fastestvpn-ovpn.XXXXXX.zip")"

cleanup() {
  rm -f "${TMP_ARCHIVE}"
}

trap cleanup EXIT INT TERM

mkdir -p "${TARGET_DIR}"

printf '[bootstrap-fastestvpn-configs] downloading %s\n' "${ARCHIVE_URL}"
curl -fsSL "${ARCHIVE_URL}" -o "${TMP_ARCHIVE}"

python3 - <<'PY' "${TMP_ARCHIVE}" "${TARGET_DIR}"
import shutil
import sys
import zipfile
from pathlib import Path

archive = Path(sys.argv[1])
target = Path(sys.argv[2])

with zipfile.ZipFile(archive) as zf:
    members = [member for member in zf.namelist() if member.lower().endswith(".ovpn")]
    if not members:
        raise SystemExit("No .ovpn files found in FastestVPN archive")
    for member in members:
        name = Path(member).name
        destination = target / name
        with zf.open(member) as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst)

print(f"Extracted {len(members)} OpenVPN profiles into {target}")
PY

find "${TARGET_DIR}" -maxdepth 1 -type f -name '*.ovpn' | sort
