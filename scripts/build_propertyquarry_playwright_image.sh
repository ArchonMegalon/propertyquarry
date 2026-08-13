#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_TAG="${PROPERTYQUARRY_PLAYWRIGHT_IMAGE:-propertyquarry-playwright:local}"
EXPECTED_VERSION="1.62.1"

docker build \
  --pull=false \
  --tag "${IMAGE_TAG}" \
  "${REPO_ROOT}/docker/propertyquarry-playwright"

ACTUAL_VERSION="$(docker run --rm "${IMAGE_TAG}" node -p "require('playwright/package.json').version")"
if [[ "${ACTUAL_VERSION}" != "${EXPECTED_VERSION}" ]]; then
  echo "playwright_version_mismatch: expected=${EXPECTED_VERSION} actual=${ACTUAL_VERSION}" >&2
  exit 1
fi

docker run --rm "${IMAGE_TAG}" npm audit --omit=dev --audit-level=high
docker run --rm "${IMAGE_TAG}" node -e \
  "const { chromium } = require('playwright'); (async () => { const browser = await chromium.launch({headless:true}); const page = await browser.newPage(); await page.setContent('<title>PropertyQuarry</title>'); if ((await page.title()) !== 'PropertyQuarry') throw new Error('browser_smoke_failed'); await browser.close(); console.log('propertyquarry_playwright_smoke=pass'); })().catch((error) => { console.error(error); process.exit(1); });"

echo "propertyquarry_playwright_image=${IMAGE_TAG}"
echo "playwright_version=${ACTUAL_VERSION}"
