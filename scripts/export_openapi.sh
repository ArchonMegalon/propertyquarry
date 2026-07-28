#!/usr/bin/env bash
set -euo pipefail

curl() {
  # shellcheck disable=SC2317
  command curl -q "$@"
}

EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
Usage:
  bash scripts/export_openapi.sh

Fetch the live OpenAPI document from the local runtime and write a timestamped
snapshot plus artifacts/openapi_latest.json. Uses EA_HOST_PORT, then .env, then 8090.
EOF
  exit 0
fi

HOST_PORT="${EA_HOST_PORT:-}"
if [[ -z "${HOST_PORT}" && -f "${EA_ROOT}/.env" ]]; then
  HOST_PORT="$(grep -E '^EA_HOST_PORT=' "${EA_ROOT}/.env" | tail -n1 | cut -d= -f2- || true)"
fi
HOST_PORT="${HOST_PORT:-8090}"
BASE="http://localhost:${HOST_PORT}"

OUT_DIR="${EA_ROOT}/artifacts"
mkdir -p "${OUT_DIR}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="${OUT_DIR}/openapi_${STAMP}.json"

curl -fsS "${BASE}/openapi.json" -o "${OUT_FILE}"
cp "${OUT_FILE}" "${OUT_DIR}/openapi_latest.json"

echo "openapi exported to: ${OUT_FILE}"
echo "latest snapshot: ${OUT_DIR}/openapi_latest.json"
