#!/usr/bin/env bash
set -euo pipefail

: "${PROPERTYQUARRY_ANDROID_KEYSTORE_PATH:?Set PROPERTYQUARRY_ANDROID_KEYSTORE_PATH}"
: "${PROPERTYQUARRY_ANDROID_KEYSTORE_PASSWORD:?Set PROPERTYQUARRY_ANDROID_KEYSTORE_PASSWORD}"
: "${PROPERTYQUARRY_ANDROID_KEY_ALIAS:?Set PROPERTYQUARRY_ANDROID_KEY_ALIAS}"
: "${PROPERTYQUARRY_ANDROID_KEY_PASSWORD:?Set PROPERTYQUARRY_ANDROID_KEY_PASSWORD}"

if [[ ! -f "${PROPERTYQUARRY_ANDROID_KEYSTORE_PATH}" || -L "${PROPERTYQUARRY_ANDROID_KEYSTORE_PATH}" ]]; then
  echo "PROPERTYQUARRY_ANDROID_KEYSTORE_PATH must be a regular, non-symlink file" >&2
  exit 2
fi

propertyquarry_mobile_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
propertyquarry_owner_uid="$(id -u)"
propertyquarry_owner_gid="$(id -g)"
propertyquarry_android_image="ghcr.io/cirruslabs/android-sdk@sha256:f9b3ea9ed2b5fc9522adae82c7b4622ab7aa54207ef532c8e615a347dca08f31"
propertyquarry_container_keystore="/run/secrets/propertyquarry-upload-keystore"
propertyquarry_bundletool_version="1.18.3"
propertyquarry_bundletool_sha256="a099cfa1543f55593bc2ed16a70a7c67fe54b1747bb7301f37fdfd6d91028e29"
propertyquarry_expected_version_code="6"
propertyquarry_expected_version_name="1.1.4"
propertyquarry_expected_min_sdk="24"
propertyquarry_expected_target_sdk="36"
propertyquarry_source_commit="$(git -C "${propertyquarry_mobile_root}" rev-parse HEAD)"
if [[ -n "$(git -C "${propertyquarry_mobile_root}" status --porcelain -- .)" ]]; then
  propertyquarry_source_dirty="true"
else
  propertyquarry_source_dirty="false"
fi

npm --prefix "${propertyquarry_mobile_root}" run test:web

docker run --rm \
  -e "PROPERTYQUARRY_BUILD_UID=${propertyquarry_owner_uid}" \
  -e "PROPERTYQUARRY_BUILD_GID=${propertyquarry_owner_gid}" \
  -e "PROPERTYQUARRY_ANDROID_KEYSTORE_PATH=${propertyquarry_container_keystore}" \
  -e PROPERTYQUARRY_ANDROID_KEYSTORE_PASSWORD \
  -e PROPERTYQUARRY_ANDROID_KEY_ALIAS \
  -e PROPERTYQUARRY_ANDROID_KEY_PASSWORD \
  -e "PROPERTYQUARRY_BUNDLETOOL_VERSION=${propertyquarry_bundletool_version}" \
  -e "PROPERTYQUARRY_BUNDLETOOL_SHA256=${propertyquarry_bundletool_sha256}" \
  -e "PROPERTYQUARRY_EXPECTED_VERSION_CODE=${propertyquarry_expected_version_code}" \
  -e "PROPERTYQUARRY_EXPECTED_VERSION_NAME=${propertyquarry_expected_version_name}" \
  -e "PROPERTYQUARRY_EXPECTED_MIN_SDK=${propertyquarry_expected_min_sdk}" \
  -e "PROPERTYQUARRY_EXPECTED_TARGET_SDK=${propertyquarry_expected_target_sdk}" \
  -e "PROPERTYQUARRY_SOURCE_COMMIT=${propertyquarry_source_commit}" \
  -e "PROPERTYQUARRY_SOURCE_DIRTY=${propertyquarry_source_dirty}" \
  -v propertyquarry-android-gradle-cache:/root/.gradle \
  -v propertyquarry-android-bundletool-cache:/var/cache/propertyquarry-bundletool \
  -v "${PROPERTYQUARRY_ANDROID_KEYSTORE_PATH}:${propertyquarry_container_keystore}:ro" \
  -v "${propertyquarry_mobile_root}:/workspace" \
  -w /workspace/android \
  "${propertyquarry_android_image}" \
  bash -lc '
    set -euo pipefail
    propertyquarry_restore_ownership() {
      chown -R "${PROPERTYQUARRY_BUILD_UID}:${PROPERTYQUARRY_BUILD_GID}" \
        /workspace/android \
        /workspace/build \
        /workspace/node_modules/@capacitor/app/android/build \
        /workspace/node_modules/@capacitor/browser/android/build \
        /workspace/node_modules/@capacitor/android/capacitor/build 2>/dev/null || true
    }
    trap propertyquarry_restore_ownership EXIT

    ./gradlew clean testReleaseUnitTest lintRelease bundleRelease --no-daemon
    propertyquarry_aab=/workspace/android/app/build/outputs/bundle/release/app-release.aab
    propertyquarry_expected_certificate=/tmp/propertyquarry-expected-upload-cert.der
    propertyquarry_actual_certificate=/tmp/propertyquarry-actual-upload-cert.der
    propertyquarry_bundletool_jar="/var/cache/propertyquarry-bundletool/bundletool-all-${PROPERTYQUARRY_BUNDLETOOL_VERSION}.jar"
    propertyquarry_bundletool_url="https://github.com/google/bundletool/releases/download/${PROPERTYQUARRY_BUNDLETOOL_VERSION}/bundletool-all-${PROPERTYQUARRY_BUNDLETOOL_VERSION}.jar"
    test -f "${propertyquarry_aab}"

    if [[ ! -f "${propertyquarry_bundletool_jar}" ]] \
      || ! printf "%s  %s\n" "${PROPERTYQUARRY_BUNDLETOOL_SHA256}" "${propertyquarry_bundletool_jar}" \
        | sha256sum --check --status
    then
      propertyquarry_bundletool_download="${propertyquarry_bundletool_jar}.download"
      rm -f "${propertyquarry_bundletool_download}"
      curl --fail --silent --show-error --location --retry 3 \
        "${propertyquarry_bundletool_url}" \
        --output "${propertyquarry_bundletool_download}"
      printf "%s  %s\n" "${PROPERTYQUARRY_BUNDLETOOL_SHA256}" "${propertyquarry_bundletool_download}" \
        | sha256sum --check --status
      mv "${propertyquarry_bundletool_download}" "${propertyquarry_bundletool_jar}"
    fi

    propertyquarry_bundletool_validation=/tmp/propertyquarry-bundletool-validation.txt
    if ! java -jar "${propertyquarry_bundletool_jar}" validate \
      --bundle="${propertyquarry_aab}" \
      >"${propertyquarry_bundletool_validation}" 2>&1
    then
      sed -n "1,240p" "${propertyquarry_bundletool_validation}" >&2
      exit 3
    fi
    propertyquarry_manifest="$(
      java -jar "${propertyquarry_bundletool_jar}" dump manifest \
        --bundle="${propertyquarry_aab}" \
        --module=base
    )"
    case "${propertyquarry_manifest}" in
      *"package=\"com.myexternalbrain.propertyquarry\""*) ;;
      *)
        echo "Signed AAB does not contain the production PropertyQuarry application id" >&2
        exit 3
        ;;
    esac
    case "${propertyquarry_manifest}" in
      *"android:versionCode=\"${PROPERTYQUARRY_EXPECTED_VERSION_CODE}\""*) ;;
      *) echo "Signed AAB has an unexpected version code" >&2; exit 3 ;;
    esac
    case "${propertyquarry_manifest}" in
      *"android:versionName=\"${PROPERTYQUARRY_EXPECTED_VERSION_NAME}\""*) ;;
      *) echo "Signed AAB has an unexpected version name" >&2; exit 3 ;;
    esac
    case "${propertyquarry_manifest}" in
      *"android:minSdkVersion=\"${PROPERTYQUARRY_EXPECTED_MIN_SDK}\""*) ;;
      *) echo "Signed AAB has an unexpected minimum SDK" >&2; exit 3 ;;
    esac
    case "${propertyquarry_manifest}" in
      *"android:targetSdkVersion=\"${PROPERTYQUARRY_EXPECTED_TARGET_SDK}\""*) ;;
      *) echo "Signed AAB has an unexpected target SDK" >&2; exit 3 ;;
    esac
    printf "bundletool_validate=pass version=%s package=%s version_code=%s version_name=%s min_sdk=%s target_sdk=%s\n" \
      "${PROPERTYQUARRY_BUNDLETOOL_VERSION}" \
      "com.myexternalbrain.propertyquarry" \
      "${PROPERTYQUARRY_EXPECTED_VERSION_CODE}" \
      "${PROPERTYQUARRY_EXPECTED_VERSION_NAME}" \
      "${PROPERTYQUARRY_EXPECTED_MIN_SDK}" \
      "${PROPERTYQUARRY_EXPECTED_TARGET_SDK}"

    jarsigner -verify "${propertyquarry_aab}"
    keytool -exportcert \
      -keystore "${PROPERTYQUARRY_ANDROID_KEYSTORE_PATH}" \
      -storetype PKCS12 \
      -storepass:env PROPERTYQUARRY_ANDROID_KEYSTORE_PASSWORD \
      -alias "${PROPERTYQUARRY_ANDROID_KEY_ALIAS}" \
      >"${propertyquarry_expected_certificate}"
    keytool -printcert \
      -rfc \
      -jarfile "${propertyquarry_aab}" \
      | openssl x509 -outform der \
      >"${propertyquarry_actual_certificate}"
    cmp -s "${propertyquarry_expected_certificate}" "${propertyquarry_actual_certificate}"
    keytool -printcert -jarfile "${propertyquarry_aab}"
    propertyquarry_aab_sha256="$(sha256sum "${propertyquarry_aab}" | cut -d " " -f 1)"
    propertyquarry_signer_sha256="$(sha256sum "${propertyquarry_actual_certificate}" | cut -d " " -f 1)"
    propertyquarry_generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    propertyquarry_evidence=/workspace/build/propertyquarry-android-release-evidence.json
    install -d -m 0755 /workspace/build
    printf "%s\n" \
      "{" \
      "  \"contract_name\": \"propertyquarry.android.release_evidence.v1\"," \
      "  \"generated_at\": \"${propertyquarry_generated_at}\"," \
      "  \"source_commit\": \"${PROPERTYQUARRY_SOURCE_COMMIT}\"," \
      "  \"source_dirty\": ${PROPERTYQUARRY_SOURCE_DIRTY}," \
      "  \"application_id\": \"com.myexternalbrain.propertyquarry\"," \
      "  \"version_code\": ${PROPERTYQUARRY_EXPECTED_VERSION_CODE}," \
      "  \"version_name\": \"${PROPERTYQUARRY_EXPECTED_VERSION_NAME}\"," \
      "  \"min_sdk\": ${PROPERTYQUARRY_EXPECTED_MIN_SDK}," \
      "  \"target_sdk\": ${PROPERTYQUARRY_EXPECTED_TARGET_SDK}," \
      "  \"artifact_path\": \"android/app/build/outputs/bundle/release/app-release.aab\"," \
      "  \"artifact_sha256\": \"${propertyquarry_aab_sha256}\"," \
      "  \"build_image\": \"ghcr.io/cirruslabs/android-sdk@sha256:f9b3ea9ed2b5fc9522adae82c7b4622ab7aa54207ef532c8e615a347dca08f31\"," \
      "  \"bundletool_version\": \"${PROPERTYQUARRY_BUNDLETOOL_VERSION}\"," \
      "  \"bundletool_sha256\": \"${PROPERTYQUARRY_BUNDLETOOL_SHA256}\"," \
      "  \"bundletool_validate\": true," \
      "  \"web_contract_tests\": true," \
      "  \"release_unit_tests\": true," \
      "  \"release_lint\": true," \
      "  \"jar_signature_verified\": true," \
      "  \"embedded_signer_matches_upload_certificate\": true," \
      "  \"upload_certificate_sha256\": \"${propertyquarry_signer_sha256}\"," \
      "  \"status\": \"upload_ready_local\"" \
      "}" \
      >"${propertyquarry_evidence}"
    printf "%s  %s\n" "${propertyquarry_aab_sha256}" "${propertyquarry_aab}"
    printf "release_evidence=%s\n" "${propertyquarry_evidence}"
  '
