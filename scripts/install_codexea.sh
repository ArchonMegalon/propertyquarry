#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT}/scripts/codexea"
DEST="${HOME}/.local/bin/codexea"

mkdir -p "$(dirname "${DEST}")"
install -m 755 "${SRC}" "${DEST}"
printf 'Installed %s -> %s\n' "${SRC}" "${DEST}"
