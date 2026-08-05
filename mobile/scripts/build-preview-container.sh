#!/usr/bin/env bash
set -euo pipefail

propertyquarry_mobile_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
propertyquarry_owner_uid="$(id -u)"
propertyquarry_owner_gid="$(id -g)"
propertyquarry_android_image="ghcr.io/cirruslabs/android-sdk@sha256:f9b3ea9ed2b5fc9522adae82c7b4622ab7aa54207ef532c8e615a347dca08f31"

docker run --rm \
  -e "PROPERTYQUARRY_BUILD_UID=${propertyquarry_owner_uid}" \
  -e "PROPERTYQUARRY_BUILD_GID=${propertyquarry_owner_gid}" \
  -v propertyquarry-android-gradle-cache:/root/.gradle \
  -v propertyquarry-android-debug-key:/root/.android \
  -v "${propertyquarry_mobile_root}:/workspace" \
  -w /workspace/android \
  "${propertyquarry_android_image}" \
  bash -lc '
    ./gradlew clean testPreviewUnitTest lintPreview assemblePreview assemblePreviewAndroidTest --no-daemon
    propertyquarry_build_result=$?
    chown -R "${PROPERTYQUARRY_BUILD_UID}:${PROPERTYQUARRY_BUILD_GID}" \
      /workspace/android \
      /workspace/node_modules/@capacitor/app/android/build \
      /workspace/node_modules/@capacitor/browser/android/build \
      /workspace/node_modules/@capacitor/android/capacitor/build 2>/dev/null || true
    exit "${propertyquarry_build_result}"
  '
